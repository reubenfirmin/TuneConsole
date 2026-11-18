"""SQL ownership of archived policy configurations."""
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE policy_history_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL CHECK(length(version)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
INDEX = 'CREATE INDEX ix_policy_history_file_version ON policy_history_files(version,id)'


def install(conn, context=None):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_history_files'").fetchone() is None:
        conn.execute(SQL)
        conn.execute(INDEX)
    else:
        validate(conn)


def validate(conn):
    for name, expected in (('policy_history_files', SQL), ('ix_policy_history_file_version', INDEX)):
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(expected):
            raise MigrationError('Incompatible historical policy file identities')


def reference(conn, version):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_history_files'").fetchone() is None:
        return None
    row = conn.execute('SELECT id,content_digest,byte_size FROM policy_history_files '
                       "WHERE version=? AND file_status='ready' ORDER BY id DESC LIMIT 1", (version,)).fetchone()
    return dict(zip(('id', 'content_digest', 'byte_size'), row)) if row else None


def reserve(conn, record):
    install(conn)
    return conn.execute('INSERT INTO policy_history_files(version,content_digest,byte_size,file_status) '
        "VALUES (?,?,?,'pending')", (record['version'], record['digest'], record['size'])).lastrowid


def complete(conn, record):
    changed = conn.execute("UPDATE policy_history_files SET file_status='ready' "
        "WHERE id=? AND version=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (record['file_id'], record['version'], record['digest'], record['size'])).rowcount
    if changed != 1:
        raise ValueError('Historical policy reservation changed before completion')
