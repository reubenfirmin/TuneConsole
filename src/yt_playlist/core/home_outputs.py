"""Replaceable Home display publications with explicit retained evidence roots."""
from dataclasses import dataclass
import hashlib
import json
import math

from . import bounded_json, group_evidence, json_cache, recommendation_outputs as files
from .activity import check_cancellable_work
from .policy_config import HOME_CONTEXTS as CONTEXTS, home_references as policy_references

SURFACES = frozenset({'home_prepared', 'home_tuned', 'home_choice', 'album_prepared', 'adaptive_group_preview',
                      'home_discovery', 'home_revisit', 'into_recently', 'trend_rollups', 'cleanup'})
MAX_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
# An explicit four-card selection is small enough for a larger bounded part.
# Avoid publishing several separately synced files for one compressed menu.
SELECTION_PART_CHARACTERS = 128 * 1024
ASCII_SELECTION_PART_CHARACTERS = 256 * 1024


@dataclass(frozen=True)
class Prepared:
    surface: str
    raw: str
    value: object
    digest: str
    parts: tuple
    built_at: float | None
    schema: int | None
    generation: str | None
    mix_regions: int
    byte_size: int


def prepare(surface, raw, at):
    if surface not in SURFACES or not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_BYTES:
        raise ValueError('Invalid Home display publication or size')
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Invalid Home display observation time')
    value = files.decode_json(raw)
    if isinstance(value, dict) and value.get(json_cache.MARKER) == json_cache.LEGACY_FORMAT:
        # Validate numbers as the bounded decoder reads them. Explicit menus
        # carry complete profiles; walking every scalar again adds foreground
        # work without establishing any additional publication invariant.
        value = bounded_json.loads(json_cache.unpack(value.get('json'), max_bytes=MAX_DECODED_BYTES), finite=True)
    elif isinstance(value, dict) and value.get(json_cache.MARKER) == json_cache.FORMAT:
        value = json_cache.decode(raw, max_bytes=MAX_DECODED_BYTES)
        _validate_numbers(value)
    metadata = value if isinstance(value, dict) else {}
    schema = metadata.get('schema')
    generation = metadata.get('generation')
    if (schema is not None and (type(schema) is not int or schema < 0)
            or generation is not None and (not isinstance(generation, str) or not 0 < len(generation.encode()) <= 1024)):
        raise ValueError('Invalid Home display identity')
    menus = metadata.get('menus', {}) if surface == 'adaptive_group_preview' else {}
    if not isinstance(menus, dict):
        raise ValueError('Invalid adaptive mix regions')
    regions = sum(len(menus.get(lane, [])) for lane in ('wheelhouse', 'explore', 'comfort', 'temporal'))
    part_characters = SELECTION_PART_CHARACTERS if surface in ('home_tuned', 'home_choice') else PART_CHARACTERS
    if surface in ('home_tuned', 'home_choice') and raw.isascii():
        # Compressed selections are ASCII. JSON quoting can at most double
        # these characters, leaving the encoded part below its 1 MiB limit.
        part_characters = ASCII_SELECTION_PART_CHARACTERS
    parts = tuple(files.encode(surface, str(position), raw[start:start + part_characters])
                  for position, start in enumerate(range(0, len(raw), part_characters)))
    if len(parts) > 128:
        raise ValueError('Home display exceeds its part count')
    encoded = raw.encode()
    return Prepared(surface, raw, value, hashlib.sha256(encoded).hexdigest(), parts, at, schema, generation, regions, len(encoded))


def _validate_numbers(value):
    pending, visited = [value], 0
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('Non-finite number in prepared output')
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        visited += 1
        if visited % 128 == 0:
            check_cancellable_work()


def _root_key(identity, field):
    return f'home-output:{identity}:{field}'


def _link_roots(conn, identity):
    for field in (*CONTEXTS, 'recommendations'):
        key = _root_key(identity, field)
        if field in CONTEXTS:
            conn.execute('INSERT INTO home_display_group_roots(publication_id,field,kind,external_key) '
                "SELECT ?,?,'prepared',external_key FROM group_evidence_owners WHERE kind='prepared' AND external_key=?",
                (identity, field, key))
        conn.execute('INSERT INTO home_display_recommendation_roots(publication_id,field,kind,external_key,generation_id) '
            "SELECT ?,?,'prepared',external_key,generation_id FROM recommendation_generation_refs "
            "WHERE kind='prepared' AND external_key=?", (identity, field, key))


def _copy_legacy_roots(conn, surface, identity):
    # Earlier registered cutovers already resolved these exact roots. Copy their
    # actual aliases and explicit unknowns; do not reinterpret old JSON now.
    for field in (*CONTEXTS, 'recommendations'):
        old, new = 'proposal:' + surface + ':' + field, _root_key(identity, field)
        if field in CONTEXTS:
            conn.execute('INSERT INTO group_evidence_owners SELECT kind,?,reference_digest,created_at '
                         "FROM group_evidence_owners WHERE kind='prepared' AND external_key=?", (new, old))
            for table in ('group_evidence_generations', 'group_evidence_versions', 'unresolved_group_evidence', 'unresolved_group_contexts'):
                columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})') if row[1] not in ('kind', 'external_key')]
                fields = ','.join(columns)
                conn.execute(f'INSERT INTO {table}(kind,external_key,{fields}) SELECT kind,?,{fields} '
                             f"FROM {table} WHERE kind='prepared' AND external_key=?", (new, old))
        conn.execute('INSERT INTO recommendation_generation_refs SELECT kind,?,generation_id '
                     "FROM recommendation_generation_refs WHERE kind='prepared' AND external_key=?", (new, old))


def _retire(conn, identity):
    from . import recommendation_custody
    conn.execute("UPDATE home_display_publications SET status='retiring' WHERE id=?", (identity,))
    conn.execute('DELETE FROM home_display_group_roots WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM home_display_recommendation_roots WHERE publication_id=?', (identity,))
    for field in (*CONTEXTS, 'recommendations'):
        group_evidence.release(conn, 'prepared', _root_key(identity, field))
        recommendation_custody.release(conn, 'prepared', _root_key(identity, field))
    conn.execute('DELETE FROM home_display_policies WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM home_display_files WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM home_display_publications WHERE id=?', (identity,))


def publish(conn, prepared, *, store=None, historical=False, input_revision=None):
    if not conn.in_transaction or not isinstance(prepared, Prepared):
        raise ValueError('Home display requires prepared output and an outer transaction')
    durable = []
    for position, encoded in enumerate(prepared.parts):
        check_cancellable_work()
        durable.append((prepared.surface, str(position), *files.publish(files.directory(conn), encoded)))
    return publish_files(conn, prepared, durable, store=store, historical=historical,
                         input_revision=input_revision)


def publish_files(conn, prepared, durable, *, store=None, historical=False, input_revision=None,
                  policies=None, resolved_contexts=None, recommendation_generations=None, roots=None):
    """Publish existing file descriptors and their SQL owners in one transaction."""
    if not conn.in_transaction:
        raise ValueError('Home display requires an outer transaction')
    policies = policy_references(prepared.value) if policies is None else policies
    identity = conn.execute('INSERT INTO home_display_publications(surface,built_at,schema_version,generation_key,mix_regions,'
        'content_digest,total_bytes,part_count,input_revision,revision_reason,has_content) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (prepared.surface, prepared.built_at, prepared.schema, prepared.generation, prepared.mix_regions,
         prepared.digest, prepared.byte_size, len(durable), input_revision,
         'legacy_not_recorded' if historical else 'publisher_not_recorded' if input_revision is None else None,
         int(prepared.raw not in ('null', '{}')))).fetchone()[0]
    conn.executemany('INSERT INTO home_display_files(publication_id,surface,chunk_key,filename,content_digest,byte_size) '
                     'VALUES (?,?,?,?,?,?)', [(identity, *row) for row in durable])
    if historical:
        _copy_legacy_roots(conn, prepared.surface, identity)
    elif roots is not None:
        group_evidence.register_prepared(conn, *roots, owner_prefix='home-output:' + str(identity))
    elif store is not None:
        from . import prepared_references
        prepared_references.replace_prepared(store, prepared.surface, prepared.value, prepared.built_at,
            owner_prefix='home-output:' + str(identity), resolved_contexts=resolved_contexts,
            recommendation_generations=recommendation_generations)
    _link_roots(conn, identity)
    for policy in sorted(policies):
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (policy,)).fetchone()
        if known is None and not historical:
            raise ValueError('Home output references an unknown policy: ' + policy)
        conn.execute('INSERT INTO home_display_policies VALUES (?,?,?,?)',
                     (identity, policy, policy if known else None, None if known else 'missing_policy'))
    conn.execute("UPDATE home_display_publications SET status='published' WHERE id=?", (identity,))
    previous = conn.execute('SELECT publication_id FROM home_display_heads WHERE surface=?', (prepared.surface,)).fetchone()
    conn.execute('INSERT INTO home_display_heads VALUES (?,?) ON CONFLICT(surface) DO UPDATE '
                 'SET publication_id=excluded.publication_id', (prepared.surface, identity))
    if previous:
        _retire(conn, previous[0])
    return identity


def encoded(conn, surface):
    captured = manifest(conn, surface)
    if captured is None:
        return None
    return read_manifest(surface, *captured)


def manifest(conn, surface):
    """Capture SQL metadata only; opening the selected files belongs to callers."""
    row = conn.execute('SELECT p.id,p.content_digest,p.total_bytes,p.part_count FROM home_display_heads h '
        'JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()
    if row is None:
        return None
    parts = conn.execute('SELECT chunk_key,filename,content_digest,byte_size FROM home_display_files '
                         'WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)', (row[0],)).fetchall()
    if len(parts) != row[3] or [part[0] for part in parts] != list(map(str, range(row[3]))):
        raise ValueError('Incomplete Home display parts')
    return row[1], row[2], parts


def read_manifest(surface, digest, size, parts):
    try:
        raw = ''.join(files.read(*part[1:], surface=surface, chunk_key=part[0]) for part in parts)
    except (OSError, ValueError):
        return None
    if len(raw.encode()) != size or hashlib.sha256(raw.encode()).hexdigest() != digest:
        raise ValueError('Home display differs from its publication digest')
    return raw


def checkpoint(conn):
    row = conn.execute('SELECT p.schema_version,p.generation_key,p.mix_regions FROM home_display_heads h '
        "JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface='adaptive_group_preview'").fetchone()
    return dict(zip(('schema', 'generation', 'mix_regions'), row)) if row else None


def present(conn, surface):
    return conn.execute('SELECT 1 FROM home_display_heads h JOIN home_display_publications p '
                        'ON p.id=h.publication_id WHERE h.surface=? AND p.has_content=1', (surface,)).fetchone() is not None


def clear(conn, surfaces=SURFACES):
    if not conn.in_transaction:
        raise ValueError('Home display invalidation requires the outer transaction')
    if set(surfaces) - SURFACES:
        raise ValueError('Unknown Home display invalidation owner')
    for surface in surfaces:
        row = conn.execute('SELECT publication_id FROM home_display_heads WHERE surface=?', (surface,)).fetchone()
        if row:
            conn.execute('DELETE FROM home_display_heads WHERE surface=?', (surface,))
            _retire(conn, row[0])
