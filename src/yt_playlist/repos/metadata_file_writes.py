"""SQL file reservations and exact revision references for runtime metadata."""
from contextlib import nullcontext

from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core.metadata_file_contract import owner_key, revision_key
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE metadata_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('alternatives','native','choices','conflicts')),
    owner_key TEXT NOT NULL CHECK(length(owner_key)>0),
    owner_id INTEGER,
    revision_key TEXT NOT NULL CHECK(length(revision_key)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
REFERENCES = '''CREATE TABLE metadata_file_references (
    kind TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    revision_key TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES metadata_file_writes(id),
    PRIMARY KEY(kind,owner_key,revision_key)
) WITHOUT ROWID'''
INDEX = 'CREATE INDEX ix_metadata_file_owner ON metadata_file_writes(kind,owner_id)'
REFERENCE_INDEX = 'CREATE INDEX ix_metadata_file_reference ON metadata_file_references(file_id)'
DEFINITIONS = {'metadata_file_writes': SQL, 'metadata_file_references': REFERENCES,
               'ix_metadata_file_owner': INDEX, 'ix_metadata_file_reference': REFERENCE_INDEX}


def install(conn, context=None):
    actual = definitions(conn, DEFINITIONS)
    for name, sql in DEFINITIONS.items():
        if name not in actual:
            conn.execute(sql)
        elif tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible runtime metadata file owner: ' + name)


def validate(conn):
    actual = definitions(conn, DEFINITIONS)
    for name, sql in DEFINITIONS.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible runtime metadata file owner: ' + name)


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_file_writes'").fetchone() is not None


def reserve(conn, record):
    install(conn)
    owner = record['owner']
    identity = owner[0] if type(owner[0]) is int else None
    return conn.execute('INSERT INTO metadata_file_writes('
        "kind,owner_key,owner_id,revision_key,content_digest,byte_size,file_status) VALUES (?,?,?,?,?,?,'pending')",
        (record['kind'], owner_key(record['kind'], owner), identity, revision_key(record['revision']),
         record['digest'], record['size'])).lastrowid


def complete(conn, record, *, kind, owner, revision):
    if (record['kind'], tuple(record['owner']), record['revision']) != (kind, tuple(owner), revision):
        raise ValueError('Runtime metadata file belongs to a different head')
    kind, owner, revision = record['kind'], owner_key(record['kind'], record['owner']), revision_key(record['revision'])
    changed = conn.execute("UPDATE metadata_file_writes SET file_status='ready' WHERE id=? AND kind=? "
        "AND owner_key=? AND revision_key=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (record['file_id'], kind, owner, revision, record['digest'], record['size'])).rowcount
    if changed != 1:
        raise ValueError('Runtime metadata file reservation changed before completion')
    conn.execute('INSERT INTO metadata_file_references(kind,owner_key,revision_key,file_id) VALUES (?,?,?,?) '
        'ON CONFLICT(kind,owner_key,revision_key) DO UPDATE SET file_id=excluded.file_id',
        (kind, owner, revision, record['file_id']))


def reference(conn, kind, owner, revision, *, store=None):
    if revision is None or not available(conn):
        return None
    key = owner_key(kind, owner)
    with keyed_reads(store, 'metadata_file_references', [key]) if store is not None else nullcontext():
        selected = conn.execute('SELECT file_id FROM metadata_file_references '
            'WHERE kind=? AND owner_key=? AND revision_key=?', (kind, key, revision_key(revision))).fetchone()
    if selected is None:
        return None
    with keyed_reads(store, 'metadata_file_writes', [selected[0]]) if store is not None else nullcontext():
        row = conn.execute("SELECT id,content_digest,byte_size FROM metadata_file_writes WHERE id=? AND file_status='ready'",
                           (selected[0],)).fetchone()
    return tuple(row) if row else None


def next_owner_id(conn, kind, table):
    if (kind, table) not in (('choices', 'metadata_runtime_choices'), ('conflicts', 'metadata_runtime_conflicts')):
        raise ValueError('Unknown runtime metadata identity owner')
    following = conn.execute('SELECT max(COALESCE((SELECT max(id) FROM '+table+'),0),'
        'COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0))+1', (table,)).fetchone()[0]
    if available(conn):
        pending = conn.execute('SELECT max(owner_id) FROM metadata_file_writes WHERE kind=?', (kind,)).fetchone()[0]
        following = max(following, (pending or 0) + 1)
    return following
