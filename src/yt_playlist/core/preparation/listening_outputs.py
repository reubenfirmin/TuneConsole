"""Prepare retained listening display documents and their ordered chunk links."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .. import listening_outputs as outputs, recommendation_outputs as files
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


HEAD = 'prepared_listening_output_generations'
CURRENT = 'prepared_listening_output_heads'
REFS = 'prepared_listening_chunk_references'
TOTALS = 'prepared_listening_output_totals'
LINKS = 'prepared_listening_selected_chunks'
SOURCE_FILTER = ("({prefix}surface IN ('listening_analytics','listening_edition') OR "
                 "({prefix}surface>='listening_chunk:' AND {prefix}surface<'listening_chunk;'))")


def _unused(*args):
    raise MigrationError('Listening output preparation requires its bounded file worker')


def definitions():
    yield HEAD, f'''CREATE TABLE {HEAD} (
        surface TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        file_surface TEXT NOT NULL CHECK(file_surface IN ('listening_chunk','listening_analytics','listening_edition')),
        chunk_key TEXT NOT NULL, filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size>0),
        source_at REAL, built_at REAL, schema_version INTEGER, edition_key TEXT, timezone TEXT,
        references_count INTEGER NOT NULL CHECK(references_count BETWEEN 0 AND {outputs.MAX_CHUNKS}),
        PRIMARY KEY(surface,generation)) WITHOUT ROWID'''
    yield REFS, f'''CREATE TABLE {REFS} (
        surface TEXT NOT NULL CHECK(surface='listening_analytics'), generation INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {outputs.MAX_CHUNKS - 1}),
        chunk_key TEXT NOT NULL, from_day TEXT NOT NULL, until_day TEXT NOT NULL,
        encoded_bytes INTEGER NOT NULL CHECK(encoded_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(surface,generation,position), UNIQUE(surface,generation,chunk_key),
        FOREIGN KEY(surface,generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''
    yield CURRENT, f'''CREATE TABLE {CURRENT} (
        surface TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(surface,generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''
    yield TOTALS, f'''CREATE TABLE {TOTALS} (
        id INTEGER PRIMARY KEY CHECK(id=1), outputs INTEGER NOT NULL DEFAULT 0 CHECK(outputs BETWEEN 0 AND {outputs.MAX_CHUNKS + 2}),
        chunks INTEGER NOT NULL DEFAULT 0 CHECK(chunks BETWEEN 0 AND {outputs.MAX_CHUNKS}),
        byte_size INTEGER NOT NULL DEFAULT 0 CHECK(byte_size BETWEEN 0 AND {outputs.MAX_PUBLICATION_BYTES}))'''
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        sides = (('OLD', -1), ('NEW', 1)) if event == 'UPDATE' else (('OLD', -1),) if event == 'DELETE' else (('NEW', 1),)
        body = f'INSERT INTO {TOTALS}(id) VALUES (1) ON CONFLICT DO NOTHING;'
        for side, factor in sides:
            body += (f'UPDATE {TOTALS} SET outputs=outputs+({factor}),'
                f"chunks=chunks+({factor})*(SELECT file_surface='listening_chunk' FROM {HEAD} WHERE surface={side}.surface AND generation={side}.generation),"
                f'byte_size=byte_size+({factor})*(SELECT byte_size FROM {HEAD} WHERE surface={side}.surface AND generation={side}.generation) WHERE id=1;')
        name = 'prepared_listening_totals_' + event.lower()
        yield name, f'CREATE TRIGGER {name} AFTER {event} ON main.{CURRENT} BEGIN {body} END'


DEFINITION = Job('listening_outputs', 79, 89, {'rec_proposals': ('surface',)}, tuple(definitions()), _unused, _unused,
    filters={'rec_proposals': SOURCE_FILTER}, projections={'rec_proposals':
        ('surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{files.MAX_BYTES + 1}),X'') AS payload", 'built_at')})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Listening output preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'listening-outputs'


def read_original(row):
    raw = files.read(row['filename'], row['digest'], row['byte_size'],
                     surface=row['file_surface'], chunk_key=row['chunk_key'])
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared listening output differs from its original digest')
    return raw


@dataclass(frozen=True)
class Plan:
    file_surface: str
    chunk_key: str
    durable: tuple
    raw_digest: str
    raw_size: int
    metadata: tuple
    units: tuple


def describe(conn, row, *, retain):
    encoded = bytes(row['payload'])
    if len(encoded) > files.MAX_BYTES:
        raise MigrationError('Listening output exceeds its preparation byte budget')
    raw = encoded.decode('utf-8')
    surface = row['surface']
    chunk = surface.removeprefix('listening_chunk:') if surface.startswith('listening_chunk:') else ''
    file_surface = 'listening_chunk' if surface.startswith('listening_chunk:') else surface
    if row['built_at'] is not None and not outputs._finite(row['built_at']):
        raise ValueError('Invalid listening output observation time')
    value = files.decode_json(raw)
    if not isinstance(value, dict):
        raise ValueError('Listening output must be an object')
    units, metadata = [], (None,) * 4
    if file_surface == 'listening_chunk':
        # Reuse the real chunk hash/shape contract; retain unselected historical
        # chunks too. Its complete edition is verified separately below.
        outputs.prepare({chunk: raw}, None, None, row['built_at'], historical=True)
    elif surface == 'listening_analytics':
        refs = value.get('chunks', [])
        if not isinstance(refs, list) or len(refs) > outputs.MAX_CHUNKS:
            raise ValueError('Invalid listening chunk index')
        seen = set()
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {'key', 'from', 'until', 'bytes'}
                    or not outputs._key(ref['key']) or ref['key'] in seen
                    or not outputs._day(ref['from']) or not outputs._day(ref['until']) or ref['from'] > ref['until']
                    or type(ref['bytes']) is not int or not 0 < ref['bytes'] <= files.MAX_BYTES):
                raise ValueError('Invalid listening chunk index')
            seen.add(ref['key'])
            units.append((ref['key'], ref['from'], ref['until'], ref['bytes']))
        schema, edition, timezone = value.get('schema'), value.get('generation'), value.get('timezone')
        built = value.get('built_at', row['built_at'])
        if (schema is not None and (type(schema) is not int or schema < 1)
                or edition is not None and not outputs._key(edition)
                or timezone is not None and (not isinstance(timezone, str) or not 0 < len(timezone.encode()) <= 1024)
                or built is not None and not outputs._finite(built)):
            raise ValueError('Invalid listening publication metadata')
        metadata = (built, schema, edition, timezone)
    durable = retain(directory(conn), files.encode(file_surface, chunk, raw))
    return Plan(file_surface, chunk, durable, hashlib.sha256(encoded).hexdigest(), len(encoded), metadata, tuple(units))


def prepare(conn, row):
    return describe(conn, row, retain=files.publish)


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {HEAD} VALUES ({",".join("?" for _ in range(15))})',
        (row['surface'], generation, plan.file_surface, plan.chunk_key, *plan.durable,
         plan.raw_digest, plan.raw_size, row['built_at'], *plan.metadata, len(plan.units)))


def copy(conn, row, generation, position, unit):
    conn.execute(f'INSERT INTO {REFS} VALUES (?,?,?,?,?,?,?)', (row['surface'], generation, position, *unit))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) '
                 'ON CONFLICT(surface) DO UPDATE SET generation=excluded.generation', (row['surface'], generation))


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {CURRENT} WHERE surface=?', (json.loads(key)[0],))


JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete)


def verify_link(conn, owner, key, row):
    chunk = conn.execute(f'SELECT g.generation,g.raw_size FROM {CURRENT} h JOIN {HEAD} g '
        'USING(surface,generation) WHERE h.surface=?', ('listening_chunk:' + row['chunk_key'],)).fetchone()
    if chunk is None or chunk['raw_size'] != row['encoded_bytes']:
        raise MigrationError('Missing selected listening chunk or mismatched original byte count: ' + row['chunk_key'])
    conn.execute(f'INSERT INTO {LINKS} VALUES (?,?,?,?,?) '
        'ON CONFLICT(surface,generation,position) DO UPDATE SET chunk_surface=excluded.chunk_surface,chunk_generation=excluded.chunk_generation',
        (row['surface'], row['generation'], row['position'], 'listening_chunk:' + row['chunk_key'], chunk['generation']))


def delete_link(conn, owner, key):
    conn.execute(f'DELETE FROM {LINKS} WHERE surface=? AND generation=? AND position=?', json.loads(key))


class LinkJob(Job):
    def _batch(self, conn, current, size):
        progress = JOB.progress(conn)
        if progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision:
            raise MigrationError('Listening chunk verification requires reconciled output preparation')
        return super()._batch(conn, current, size)


LINK_JOB = LinkJob('listening_output_links', 79, 89, {REFS: ('surface', 'generation', 'position')}, (
    (LINKS, f'''CREATE TABLE {LINKS} (
        surface TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL,
        chunk_surface TEXT NOT NULL, chunk_generation INTEGER NOT NULL,
        PRIMARY KEY(surface,generation,position),
        FOREIGN KEY(surface,generation,position) REFERENCES {REFS}(surface,generation,position),
        FOREIGN KEY(chunk_surface,chunk_generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''),
), verify_link, delete_link, dependencies=(CURRENT, DEFINITION.state), filters={REFS:
    f'({{prefix}}surface,{{prefix}}generation) IN (SELECT surface,generation FROM {CURRENT})'})


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('files', 'links'))
    args, remaining = parser.parse_known_args(argv)
    return run(JOB if args.job == 'files' else LINK_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
