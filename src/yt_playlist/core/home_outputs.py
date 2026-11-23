"""Replaceable Home display publications with explicit retained evidence roots."""
from dataclasses import dataclass
import hashlib
import json
import math

from . import bounded_json, fao, group_evidence, json_cache, publication_files, recommendation_custody
from . import recommendation_outputs as files
from .activity import check_cancellable_work
from .policy_config import home_references as policy_references
from .recommendation_content import read_preview_part

SURFACES = frozenset({'home_prepared', 'home_tuned', 'home_choice', 'album_prepared', 'adaptive_group_preview',
                      'home_discovery', 'home_revisit', 'into_recently', 'trend_rollups', 'cleanup'})
PREFIX = publication_files.HOME
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


@dataclass(frozen=True)
class Manifest:
    publication_id: int
    digest: str
    size: int
    parts: list
    schema: int | None = None


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
    elif isinstance(value, dict) and value.get(json_cache.MARKER) in (json_cache.FORMAT, json_cache.HOME_MENUS):
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
    if surface in ('home_tuned', 'home_prepared') and raw.startswith(
            '{"' + json_cache.MARKER + '":"' + json_cache.HOME_MENUS + '"'):
        sections = json_cache.home_menu_parts(raw)
    else:
        sections = (raw[start:start + part_characters] for start in range(0, len(raw), part_characters))
    parts = tuple(files.encode(surface, str(position), section) for position, section in enumerate(sections))
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


def publish_files(conn, prepared, durable, *, input_revision=None, policies=None, roots=None):
    """Publish existing file descriptors and their SQL owners in one transaction."""
    if not conn.in_transaction:
        raise ValueError('Home display requires an outer transaction')
    policies = policy_references(prepared.value) if policies is None else policies
    identity = publication_files.next_identity(conn, PREFIX)
    groups, recommendations = [], []
    if roots is not None:
        kind, _, operations, generations = roots
        groups = group_evidence.display_contexts(conn, kind, identity, operations)
        captured = {'recommendations': generations}
        captured.update((name.rsplit(':', 1)[1], references) for name, _, unknown, references in operations
                        if unknown is None)
        for field, generation_ids in sorted(captured.items()):
            for generation in generation_ids:
                recommendation_custody.require_published(conn, generation)
                recommendations.append((field, kind, _root_key(identity, field), generation))
    policy_roots = []
    for policy in sorted(policies):
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (policy,)).fetchone()
        if known is None:
            raise ValueError('Home output references an unknown policy: ' + policy)
        policy_roots.append((policy, policy, None))
    if [(part[0], part[1]) for part in durable] != [(prepared.surface, str(i)) for i in range(len(prepared.parts))]:
        raise ValueError('Home display parts differ from the prepared manifest')
    publication_files.save(conn, PREFIX + prepared.surface, {
        'id': identity, 'surface': prepared.surface, 'built_at': prepared.built_at, 'schema_version': prepared.schema,
        'generation_key': prepared.generation, 'mix_regions': prepared.mix_regions, 'content_digest': prepared.digest,
        'total_bytes': prepared.byte_size, 'part_count': len(durable), 'input_revision': input_revision,
        'revision_reason': 'publisher_not_recorded' if input_revision is None else None,
        'has_content': int(prepared.raw not in ('null', '{}')),
        'files': json.loads(publication_files.manifest(conn, 'home_display_files', durable)),
        'group_roots': groups, 'recommendation_roots': recommendations, 'policy_roots': policy_roots,
    })
    return identity


def encoded(conn, surface):
    captured = manifest(conn, surface)
    if captured is None:
        return None
    return read_manifest(surface, captured.digest, captured.size, captured.parts)


def manifest(conn, surface):
    """Capture SQL metadata only; opening the selected files belongs to callers."""
    row = publication_files.fields(conn, PREFIX + surface, 'id', 'content_digest', 'total_bytes',
                                   'part_count', 'files', 'schema_version')
    if row is None:
        return None
    identity, digest, size, count, encoded, schema = row
    parts = [tuple(part[2:6]) for part in json.loads(encoded)]
    if [part[0] for part in parts] != list(map(str, range(count))):
        raise ValueError('Incomplete Home display parts')
    return Manifest(identity, digest, size, parts, schema)


def read_parts(surface, parts):
    return [files.read(*part[1:], surface=surface, chunk_key=part[0]) for part in parts]


def read_manifest(surface, digest, size, parts):
    try:
        if surface == 'adaptive_group_preview':
            return ''.join(read_preview_part(filename, length, chunk) for chunk, filename, _, length in parts)
        raw = ''.join(read_parts(surface, parts))
    except (OSError, ValueError, fao.FileChanged):
        return None
    encoded = raw.encode()
    if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('Home display differs from its publication digest')
    return raw


def checkpoint(conn):
    row = publication_files.fields(conn, PREFIX + 'adaptive_group_preview', 'schema_version',
                                   'generation_key', 'mix_regions', 'id')
    return dict(zip(('schema', 'generation', 'mix_regions', 'publication_id'), row)) if row else None


def preview_recommendations(conn, generation, schema):
    return [row[0] for row in conn.execute(f'''
        SELECT r.value->>3 FROM {publication_files.MANIFESTS} p,json_each(p.value,'$.recommendation_roots') r
        WHERE p.key=? AND p.value->>'generation_key'=?
          AND p.value->>'schema_version'=? AND p.value->>'mix_regions'>0 AND r.value->>0='recommendations'
    ''', (PREFIX + 'adaptive_group_preview', generation, schema))]


def present(conn, surface):
    row = publication_files.fields(conn, PREFIX + surface, 'has_content')
    return bool(row and row[0])


def clear(conn, surfaces=SURFACES):
    if not conn.in_transaction:
        raise ValueError('Home display invalidation requires the outer transaction')
    if set(surfaces) - SURFACES:
        raise ValueError('Unknown Home display invalidation owner')
    for surface in surfaces:
        publication_files.clear(conn, PREFIX + surface)
