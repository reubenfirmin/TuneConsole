"""SQL-owned immutable group document receipts."""
from yt_playlist.core.group_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE group_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('definition','generation','lineage')),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='group_file_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible musical group file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='group_file_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible musical group file identities')


def reserve(conn, records):
    install(conn)
    return [conn.execute('INSERT INTO group_file_writes(kind,content_digest,byte_size,file_status) '
        "VALUES (?,?,?,'pending')", (record['kind'], record['digest'], record['size'])).lastrowid
        for record in records]


def complete(conn, kind, reference):
    from yt_playlist.core.group_documents import HEADER
    filename, digest, size, offset = reference
    identity = record_id(filename)
    if identity is None or offset != len(HEADER):
        raise ValueError('Musical group document does not match its reserved identity')
    changed = conn.execute("UPDATE group_file_writes SET file_status='ready' "
        'WHERE id=? AND kind=? AND content_digest=? AND byte_size=? '
        "AND file_status='pending'", (identity, kind, digest, size)).rowcount
    if changed != 1:
        raise ValueError('Musical group file reservation changed before completion')
