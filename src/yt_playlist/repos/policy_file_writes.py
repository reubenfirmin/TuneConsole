"""SQL receipts for current policy files; callers own their SQL transactions."""
from yt_playlist.core.policy_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE policy_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body INTEGER NOT NULL CHECK(body IN (0,1)),
    version TEXT NOT NULL CHECK(length(version)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='policy_file_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible policy file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='policy_file_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible policy file identities')


def reserve(conn, record):
    install(conn)
    return conn.execute('INSERT INTO policy_file_writes(body,version,content_digest,byte_size,file_status) '
        "VALUES (?,?,?,?,'pending')", (int(record['body']), record['version'], record['digest'], record['size'])).lastrowid


def complete(conn, record):
    if record_id(record['filename']) != record['file_id'] or record['offset'] != 0:
        raise ValueError('Policy filename does not match its reserved identity')
    changed = conn.execute("UPDATE policy_file_writes SET file_status='ready' "
        'WHERE id=? AND body=? AND version=? AND content_digest=? AND byte_size=? '
        "AND file_status='pending'", (record['file_id'], int(record['body']), record['version'],
                                     record['digest'], record['size'])).rowcount
    if changed != 1:
        raise ValueError('Policy file reservation changed before completion')
