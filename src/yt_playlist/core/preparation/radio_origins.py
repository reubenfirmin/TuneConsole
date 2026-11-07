"""Prepare retained Radio playlist origins without inventing account ownership."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .. import entity_names, radio_provenance, recommendation_outputs as files
from ..schema_contract import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 16384
MAX_PARTS = 256
PART_CHARACTERS = 64 * 1024
KEYS = radio_provenance.KEYS
HEAD = 'prepared_radio_origin_generations'
PARTS = 'prepared_radio_origin_original_parts'
ORIGINS = 'prepared_radio_origins'
CURRENT = 'prepared_radio_origin_heads'


def _unused(*args):
    raise MigrationError('Radio origin preparation requires its bounded file worker')


DEFINITION = Job('radio_origins', 79, 93, {'settings': ('key',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        source TEXT NOT NULL CHECK(source IN {KEYS!r}), generation INTEGER NOT NULL CHECK(generation>=0),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 0 AND {MAX_PARTS}),
        item_count INTEGER NOT NULL CHECK(item_count BETWEEN 0 AND {MAX_ITEMS}),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','list','scalar')),
        PRIMARY KEY(source,generation)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        source TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,generation,position), FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
    (ORIGINS, f'''CREATE TABLE {ORIGINS} (
        source TEXT NOT NULL, generation INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}),
        provider_playlist_id TEXT NOT NULL CHECK(length(provider_playlist_id)>0),
        playlist_scope_id TEXT, unresolved_reason TEXT,
        CHECK((playlist_scope_id IS NOT NULL AND unresolved_reason IS NULL)
           OR (playlist_scope_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason IN ('missing_scope','ambiguous_scope'))),
        PRIMARY KEY(source,generation,position),
        FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        source TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('playlist_scopes',),
    filters={'settings': '{prefix}key IN ' + repr(KEYS)}, projections={'settings':
        ('key', f"CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') END AS value")})


@dataclass(frozen=True)
class Plan:
    digest: str
    size: int
    part_count: int
    item_count: int
    kind: str
    units: tuple


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Radio origin preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'radio-origins'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
        (row['source'], row['generation'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared Radio origin original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface='radio_origin', chunk_key=f'{row["source"]}:{part[0]}') for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared Radio origin differs from its original digest')
    return None if row['source_kind'] == 'null' else raw


def decode(source, original):
    raw = original.encode('utf-8') if original is not None else b''
    if len(raw) > MAX_BYTES:
        raise MigrationError('Radio origins exceed their preparation byte budget')
    text = raw.decode('utf-8')
    if original is None or not text:
        values, kind = [], 'null' if original is None else 'blank'
    elif source == KEYS[0]:
        values, kind = files.decode_json(text), 'list'
        if not isinstance(values, list):
            raise ValueError('Legacy Radio origins must be an ordered list')
    else:
        values, kind = [text], 'scalar'
    if len(values) > MAX_ITEMS or any(not isinstance(value, str) or not value for value in values):
        raise ValueError('Radio origins require bounded exact provider playlist IDs')
    return text, values, kind


def prepare(conn, row):
    original = bytes(row['value']).decode('utf-8') if row['value'] is not None else None
    text, values, kind = decode(row['key'],original)
    raw = text.encode('utf-8')
    durable = [('file', position, files.publish(directory(conn), files.encode('radio_origin',
        f'{row["key"]}:{position}', text[start:start + PART_CHARACTERS])))
        for position, start in enumerate(range(0, len(text), PART_CHARACTERS))]
    if len(durable) > MAX_PARTS:
        raise MigrationError('Radio origins exceed their original part budget')
    return Plan(hashlib.sha256(raw).hexdigest(), len(raw), len(durable), len(values), kind,
                tuple(durable + [('origin', position, value) for position, value in enumerate(values)]))


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?)',
        (row['key'], generation, plan.digest, plan.size, plan.part_count, plan.item_count, plan.kind))


def copy(conn, row, generation, position, unit):
    kind, rank, value = unit
    if kind == 'file':
        conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?)', (row['key'], generation, rank, *value))
        return
    conn.execute(f'INSERT INTO {ORIGINS} VALUES (?,?,?,?,?,?)',
        (row['key'], generation, rank, value, *reference(conn,value)))


def reference(conn, provider):
    scopes = entity_names.owner(conn, 'playlist_scopes')
    # The provider/kind uniqueness index admits at most one shared scope. The
    # account-scoped lookup uses its provider/account index and stops at two
    # witnesses; ambiguity cannot turn into an unbounded list or a guessed owner.
    matches = conn.execute(f'SELECT {scopes.column("id")} FROM {scopes.table} '
        f'WHERE {scopes.column("provider_playlist_id")}=? LIMIT 2', (provider,)).fetchall()
    return (matches[0][0] if len(matches) == 1 else None,
        None if len(matches) == 1 else 'missing_scope' if not matches else 'ambiguous_scope')


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation',
                 (row['key'], generation))


def delete(conn, owner, key):
    source = json.loads(key)[0]
    if source in KEYS:
        conn.execute(f'DELETE FROM {CURRENT} WHERE source=?', (source,))


def repair_files(conn,progress):
    try:
        conn.execute('BEGIN')
        if DEFINITION.progress(conn) != progress:
            return
        heads = conn.execute(f'SELECT g.* FROM {CURRENT} h JOIN {HEAD} g USING(source,generation)').fetchall()
        sources = dict(conn.execute('SELECT key,CASE WHEN value IS NULL THEN NULL ELSE '
            "COALESCE(substr(CAST(value AS BLOB),1,?),X'') END FROM main.settings WHERE key IN (?,?,?,?)",
            (MAX_BYTES+1,*KEYS)))
        manifests = {head['source']:list(map(tuple,conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
            'WHERE source=? AND generation=? ORDER BY position',(head['source'],head['generation'])))) for head in heads}
    finally:
        conn.rollback()
    for head in heads:
        source = head['source']
        if source not in sources:
            continue
        original = bytes(sources[source]).decode('utf-8') if sources[source] is not None else None
        text,_,kind = decode(source,original)
        if kind != head['source_kind'] or hashlib.sha256(text.encode()).hexdigest() != head['raw_digest']:
            continue
        plan = prepare(conn,{'key':source,'value':sources[source]})
        actual = [(position,*value) for kind,position,value in plan.units if kind == 'file']
        if actual != manifests[source]:
            raise MigrationError('Radio origin recovery differs from its retained file manifest')


JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete, repair_files=repair_files)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
