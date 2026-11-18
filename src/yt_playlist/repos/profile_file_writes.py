"""Committed profile pack reservations and head schema; SQL only."""
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE profile_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('recording','source')),
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
COLUMN = 'file_id INTEGER REFERENCES profile_file_writes(id)'


def head_definitions():
    yield ('recording_profile_files', 'CREATE TABLE recording_profile_files ('
        'recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0))')
    yield ('source_profile_files', 'CREATE TABLE source_profile_files ('
        'source_key TEXT PRIMARY KEY NOT NULL REFERENCES rec_musical_source_profiles(source_key) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0)) WITHOUT ROWID')


def native_definition(sql):
    return sql.replace('CHECK(byte_size>0))', 'CHECK(byte_size>0),' + COLUMN + ')')


def native(conn):
    return any(row[1] == 'file_id' for row in conn.execute('PRAGMA main.table_xinfo(recording_profile_files)'))


def install(conn, context=None):
    actual = definitions(conn, ('profile_file_writes', 'recording_profile_files', 'source_profile_files'))
    if 'profile_file_writes' not in actual:
        conn.execute(SQL)
    elif tokens(actual['profile_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible profile file identities')
    for kind in ('recording', 'source'):
        table = kind + '_profile_files'
        if 'file_id' not in tokens(actual[table]):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {COLUMN}')


def validate(conn):
    expected = {'profile_file_writes': SQL}
    expected.update((name, native_definition(sql)) for name, sql in head_definitions())
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible profile file identity owner: ' + name)


def reserve(conn, records):
    install(conn)
    return [conn.execute("INSERT INTO profile_file_writes(kind,content_digest,byte_size,file_status) "
        "VALUES (?,?,?,'pending')", (record['kind'], record['digest'], record['size'])).lastrowid
        for record in records]


def complete(conn, kind, references):
    packs = {}
    for key, owner, digest, logical_size, version, body_digest, identity, size in references:
        if type(identity) is not int or identity <= 0:
            raise ValueError('Invalid profile file reservation')
        facts = digest, size
        if packs.setdefault(identity, facts) != facts:
            raise ValueError('Conflicting profile file reservation')
    for identity, (digest, size) in packs.items():
        changed = conn.execute("UPDATE profile_file_writes SET file_status='ready' "
            "WHERE id=? AND kind=? AND content_digest=? AND byte_size=? AND file_status='pending'",
            (identity, kind, digest, size)).rowcount
        if changed != 1:
            raise ValueError('Profile file reservation changed before completion')
