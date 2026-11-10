"""Retain exact frozen album source choices independently of regular DDL."""
import hashlib
import json
from pathlib import Path

from .. import recommendation_outputs as files
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import album_occurrences, document_plan
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
PREFIX = 'prepared_album_share_'


def table(suffix):
    return PREFIX + suffix


def definitions():
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        source TEXT NOT NULL,preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),title TEXT NOT NULL,
        album_id INTEGER,resolution_reason TEXT CHECK(resolution_reason='missing_release'),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512),member_count INTEGER NOT NULL CHECK(member_count>=0),
        unit_count INTEGER NOT NULL CHECK(unit_count>=1),plan_filename TEXT NOT NULL,plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision),CHECK((album_id IS NULL)=(resolution_reason IS NOT NULL))) WITHOUT ROWID'''
    yield table('heads'), f'''CREATE TABLE {table('heads')} (
        source TEXT PRIMARY KEY,preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {table('generations')}(source,preparation_revision)) WITHOUT ROWID'''
    yield table('parts'), f'''CREATE TABLE {table('parts')} (
        source TEXT NOT NULL,preparation_revision INTEGER NOT NULL,position INTEGER NOT NULL CHECK(position>=0),
        filename TEXT NOT NULL,content_digest TEXT NOT NULL CHECK(length(content_digest)=64),byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {table('generations')}(source,preparation_revision)) WITHOUT ROWID'''
    yield table('items'), f'''CREATE TABLE {table('items')} (
        source TEXT NOT NULL,preparation_revision INTEGER NOT NULL,position INTEGER NOT NULL CHECK(position>=0),
        raw_source_ref TEXT NOT NULL CHECK(length(trim(raw_source_ref))>0),entry_id INTEGER,recording_id INTEGER,
        captured_title TEXT,captured_credit TEXT,resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved')),
        PRIMARY KEY(source,preparation_revision,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {table('generations')}(source,preparation_revision),
        CHECK((recording_id IS NULL)=(resolution_status='unresolved'))) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Album share preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'album-shares'


def prepare(conn, row, *, revision, offset, rebuild=False):
    source = row['source']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table("generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared album share has no durable plan')
        return document_plan.load(source, previous)
    fields, units = describe(conn, row)
    result = document_plan.build(directory(conn), source, fields, units)
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt album share differs from its captured checkpoint')
    return result


def describe(conn, row, *, retain=None):
    source = row['source']
    retain = retain or files.publish
    originals = {'browse_id': source}
    for field in ('title','video_ids_json'):
        value = bytes(row[field]) if row[field] is not None else None
        if value is None or len(value) > MAX_SOURCE_BYTES:
            raise ValueError('Album share source exceeds its preparation budget or has unsupported null text')
        originals[field] = value.decode()
    raw = canonical(originals)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Album share source exceeds its preparation byte budget')
    videos = json.loads(originals['video_ids_json'])
    if not isinstance(videos, list) or any(not isinstance(video, str) or not video.strip() for video in videos):
        raise ValueError('Album share requires ordered nonempty source references')
    album = conn.execute("SELECT album_id FROM main.album_provider_ids WHERE provider='youtube' AND provider_id=?", (source,)).fetchone()
    text = raw.decode()
    fields = {'title': originals['title'], 'album_id': album[0] if album else None, 'resolution_reason': None if album else 'missing_release',
        'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
        'part_count': max(1, (len(text)+PART_CHARACTERS-1)//PART_CHARACTERS), 'member_count': len(videos)}
    root = directory(conn)
    def units():
        for position in range(fields['part_count']):
            yield 'parts', (position, *retain(root, files.encode(source, 'original:' + str(position), text[position*PART_CHARACTERS:(position+1)*PART_CHARACTERS])))
        for position, video in enumerate(videos):
            found = conn.execute('SELECT id FROM main.catalog_entries WHERE video_id=?', (video,)).fetchone()
            observed = album_occurrences.entry(conn, found[0]) if found else None
            yield 'items', (position, video, observed['id'] if observed else None, observed['recording_id'] if observed else None,
                observed['title'] if observed else None, observed['artist'] if observed else None, 'resolved' if observed else 'unresolved')
    return fields, units()


def begin(conn, row, revision, plan):
    values = {'source': row['source'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table("generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(conn, row, revision, position, unit):
    suffix, values = unit
    if suffix not in ('parts','items'):
        raise MigrationError('Unknown prepared album share member')
    conn.execute(f'INSERT INTO {table(suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})', (row['source'], revision, *values))


def complete(conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table("heads")} VALUES (?,?) ON CONFLICT(source) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['source'], revision))


def delete(conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table("heads")} WHERE source=?', (source,))


def resume_read(conn, owner, key):
    source, = json.loads(key)
    return conn.execute('SELECT browse_id AS source FROM main.album_share_snapshots WHERE browse_id=? AND json_array(browse_id)=?', (source, key)).fetchone()


def head(conn, source):
    return conn.execute(f'SELECT g.* FROM {table("heads")} h JOIN {table("generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (source,)).fetchone()


def read_original(conn, generation):
    source = generation['source']
    rows = list(conn.execute(f'SELECT * FROM {table("parts")} WHERE source=? AND preparation_revision=? ORDER BY position', (source, generation['preparation_revision'])))
    if len(rows) != generation['part_count'] or [row['position'] for row in rows] != list(range(len(rows))):
        raise MigrationError('Prepared album share original has incomplete parts')
    text = ''.join(files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                             chunk_key='original:' + str(row['position'])) for row in rows)
    raw = text.encode()
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared album share differs from its captured bytes')
    return json.loads(text)


DEFINITION = Job('album_shares', 79, 98, {'album_share_snapshots': ('browse_id',)}, tuple(definitions()), None, None,
    dependencies=('albums','album_provider_ids','catalog_entries','youtube_items','unresolved_items','recordings'), refresh_highwater=False,
    projections={'album_share_snapshots': ('browse_id AS source', *(
        f'CAST(substr(CAST({field} AS BLOB),1,{MAX_SOURCE_BYTES+1}) AS BLOB) AS {field}' for field in ('title','video_ids_json')))})
JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete, revision_aware=True, resume_read=resume_read)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
