"""Atomic listening editions and their bounded, directly owned display files.

Source listening evidence remains in its existing owners. These publications
retain only display output and the exact index needed to fetch its chunks.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path

from . import recommendation_outputs as files
from .activity import check_cancellable_work

SURFACES = ('listening_analytics', 'listening_edition')
MAX_CHUNKS = 4096
MAX_PUBLICATION_BYTES = 128 * 1024 * 1024


def owns(surface):
    return surface in SURFACES or surface.startswith('listening_chunk:')


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='listening_output_publications'").fetchone() is not None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _day(value):
    return isinstance(value, str) and date.fromisoformat(value).isoformat() == value


def _key(value):
    return isinstance(value, str) and len(value) == 24 and all(c in '0123456789abcdef' for c in value)


@dataclass(frozen=True)
class Publication:
    # Raw JSON bytes are retained: chunk URLs hash this representation, rather
    # than a newly encoded interpretation of it.
    outputs: tuple
    memberships: tuple
    schema_version: int | None
    generation: str | None
    timezone: str | None
    built_at: float | None
    input_revision: int | None
    output_times: tuple


def prepare(chunks, manifest, edition, now, *, input_revision=None, historical=False, output_times=None):
    if (not isinstance(chunks, dict) or len(chunks) > MAX_CHUNKS
            or now is not None and not _finite(now)
            or input_revision is not None and (type(input_revision) is not int or input_revision < 0)):
        raise ValueError('Invalid listening publication admission')
    outputs, total, memberships = [], 0, []
    parsed_manifest = files.decode_json(manifest) if manifest is not None else None
    if parsed_manifest is not None and not isinstance(parsed_manifest, dict):
        raise ValueError('Invalid listening manifest')
    refs = parsed_manifest.get('chunks', []) if parsed_manifest is not None else []
    if not isinstance(refs, list) or len(refs) > MAX_CHUNKS:
        raise ValueError('Invalid listening chunk index')
    seen = set()
    for position, ref in enumerate(refs):
        if (not isinstance(ref, dict) or set(ref) != {'key', 'from', 'until', 'bytes'}
                or not _key(ref['key']) or ref['key'] in seen or ref['key'] not in chunks
                or not _day(ref['from']) or not _day(ref['until']) or ref['from'] > ref['until']
                or type(ref['bytes']) is not int or not 0 < ref['bytes'] <= files.MAX_BYTES):
            raise ValueError('Invalid or missing selected listening chunk')
        raw = chunks[ref['key']]
        if not isinstance(raw, str) or len(raw.encode()) != ref['bytes']:
            raise ValueError('Listening chunk byte count differs from its index')
        seen.add(ref['key'])
        memberships.append((position, ref['key'], ref['from'], ref['until'], ref['bytes']))
    if not historical and seen != set(chunks):
        raise ValueError('Unselected listening chunks cannot enter a new publication')
    for key, raw in chunks.items():
        if (not _key(key) or not isinstance(raw, str)
                or hashlib.sha256(raw.encode()).hexdigest()[:24] != key):
            raise ValueError('Listening chunk content differs from its URL')
        value = files.decode_json(raw)
        if (not isinstance(value, dict) or not isinstance(value.get('rows'), list)
                or not isinstance(value.get('columns'), list) or not isinstance(value.get('catalog'), dict)):
            raise ValueError('Invalid listening display chunk')
        outputs.append(('listening_chunk', key, files.encode('listening_chunk', key, raw)))
    for surface, raw in zip(SURFACES, (manifest, edition)):
        if raw is not None:
            if not isinstance(raw, str) or not isinstance(files.decode_json(raw), dict):
                raise ValueError('Invalid listening display document')
            outputs.append((surface, '', files.encode(surface, '', raw)))
    for _, _, encoded in outputs:
        total += len(encoded)
    if total > MAX_PUBLICATION_BYTES:
        raise ValueError('Listening publication exceeds its total byte budget')
    metadata = parsed_manifest or {}
    schema = metadata.get('schema')
    generation = metadata.get('generation')
    timezone = metadata.get('timezone')
    built = metadata.get('built_at', now)
    if (schema is not None and (type(schema) is not int or schema < 1)
            or generation is not None and not _key(generation)
            or timezone is not None and (not isinstance(timezone, str) or not 0 < len(timezone.encode()) <= 1024)
            or built is not None and not _finite(built)):
        raise ValueError('Invalid listening publication metadata')
    times = []
    for surface, key, _ in outputs:
        at = (output_times or {}).get('listening_chunk:' + key if key else surface, now)
        if at is not None and not _finite(at):
            raise ValueError('Invalid listening file observation time')
        times.append((surface, key, at))
    return Publication(tuple(outputs), tuple(memberships), schema, generation, timezone, built, input_revision, tuple(times))


def publish(conn, publication):
    if not conn.in_transaction:
        raise ValueError('Listening publication requires the guarded outer transaction')
    if not isinstance(publication, Publication):
        raise ValueError('Listening publication was not prepared')
    durable = []
    times = {(surface, key): at for surface, key, at in publication.output_times}
    for surface, key, encoded in publication.outputs:
        check_cancellable_work()
        durable.append((surface, key, *files.publish(files.directory(conn), encoded), times[surface, key]))
    # No SQL owner changes until every admitted file is durable.
    identity = conn.execute('INSERT INTO listening_output_publications(schema_version,generation_key,timezone, built_at,'
        'input_revision,revision_reason,output_count,chunk_count) VALUES (?,?,?,?,?,?,?,?) RETURNING id',
        (publication.schema_version, publication.generation, publication.timezone, publication.built_at,
         publication.input_revision, 'legacy_not_recorded' if publication.input_revision is None else None,
         len(durable), len(publication.memberships))).fetchone()[0]
    conn.executemany('INSERT INTO listening_output_files(publication_id,surface,chunk_key,filename,content_digest,byte_size,built_at) '
                     'VALUES (?,?,?,?,?,?,?)', [(identity, *row) for row in durable])
    conn.executemany('INSERT INTO listening_output_chunks(publication_id,position,chunk_key,from_day,until_day,encoded_bytes) '
                     'VALUES (?,?,?,?,?,?)', [(identity, *row) for row in publication.memberships])
    conn.execute("UPDATE listening_output_publications SET status='published' WHERE id=?", (identity,))
    current = conn.execute("SELECT publication_id FROM listening_output_heads WHERE slot='current'").fetchone()
    conn.execute('DELETE FROM listening_output_heads')
    conn.execute("INSERT INTO listening_output_heads VALUES ('current',?)", (identity,))
    if current:
        conn.execute("INSERT INTO listening_output_heads VALUES ('previous',?)", (current[0],))
    for (retired,) in conn.execute('SELECT id FROM listening_output_publications WHERE id NOT IN '
                                  '(SELECT publication_id FROM listening_output_heads)').fetchall():
        conn.execute("UPDATE listening_output_publications SET status='retiring' WHERE id=?", (retired,))
        conn.execute('DELETE FROM listening_output_chunks WHERE publication_id=?', (retired,))
        conn.execute('DELETE FROM listening_output_files WHERE publication_id=?', (retired,))
        conn.execute('DELETE FROM listening_output_publications WHERE id=?', (retired,))
    return identity


def _file(conn, surface):
    if surface in SURFACES:
        row = conn.execute('SELECT f.filename,f.content_digest,f.byte_size FROM listening_output_heads h '
            'JOIN listening_output_files f ON f.publication_id=h.publication_id '
            "WHERE h.slot='current' AND f.surface=? AND f.chunk_key=''", (surface,)).fetchone()
        key = ''
    elif surface.startswith('listening_chunk:'):
        key = surface.removeprefix('listening_chunk:')
        if not _key(key):
            return None, surface, key
        row = conn.execute('SELECT f.filename,f.content_digest,f.byte_size FROM listening_output_heads h '
            'JOIN listening_output_files f ON f.publication_id=h.publication_id '
            "WHERE f.surface='listening_chunk' AND f.chunk_key=? ORDER BY h.slot LIMIT 1", (key,)).fetchone()
        surface = 'listening_chunk'
    else:
        raise ValueError('Unknown listening output surface')
    return row, surface, key


def present(conn, surface):
    """Check the selected file descriptor without reading a display document."""
    row = _file(conn, surface)[0]
    return row is not None and Path(row[0]).is_file()


def encoded(conn, surface):
    row, surface, key = _file(conn, surface)
    if row is None:
        return None
    try:
        return files.read(*row, surface=surface, chunk_key=key)
    except (OSError, ValueError):
        return None


def publish_edition(conn, raw, now):
    """Refresh editorial framing while retaining its current indexed history."""
    manifest = encoded(conn, 'listening_analytics')
    chunks = {ref['key']: encoded(conn, 'listening_chunk:' + ref['key'])
              for ref in (json.loads(manifest).get('chunks', []) if manifest else [])}
    return publish(conn, prepare(chunks, manifest, raw, now))


def publish_document(conn, surface, raw, now):
    if surface == 'listening_edition':
        return publish_edition(conn, raw, now)
    if surface != 'listening_analytics':
        raise ValueError('Listening chunks must publish with their complete manifest')
    refs = json.loads(raw).get('chunks', [])
    chunks = {ref['key']: encoded(conn, 'listening_chunk:' + ref['key']) for ref in refs}
    return publish(conn, prepare(chunks, raw, encoded(conn, 'listening_edition'), now))


def clear(conn):
    if not conn.in_transaction:
        raise ValueError('Listening display invalidation requires the outer transaction')
    conn.execute('DELETE FROM listening_output_heads')
    for (identity,) in conn.execute('SELECT id FROM listening_output_publications').fetchall():
        conn.execute("UPDATE listening_output_publications SET status='retiring' WHERE id=?", (identity,))
        conn.execute('DELETE FROM listening_output_chunks WHERE publication_id=?', (identity,))
        conn.execute('DELETE FROM listening_output_files WHERE publication_id=?', (identity,))
        conn.execute('DELETE FROM listening_output_publications WHERE id=?', (identity,))
