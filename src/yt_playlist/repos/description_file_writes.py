"""Committed identities for immutable paid-description outcomes."""
from yt_playlist.core.description_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE description_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES album_description_requests(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK(attempt_number>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 262144),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    actual = definitions(conn, ('description_file_writes',))
    if not actual:
        conn.execute(SQL)
    elif tokens(actual['description_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible paid-description file identities')


def validate(conn):
    actual = definitions(conn, ('description_file_writes',))
    if not actual or tokens(actual['description_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible paid-description file identities')


def reserve(conn, request_id, attempt, digest, size):
    install(conn)
    return conn.execute('INSERT INTO description_file_writes('
        "request_id,attempt_number,content_digest,byte_size,file_status) VALUES (?,?,?,?,'pending')",
        (request_id, attempt, digest, size)).lastrowid


def complete(conn, identity, request_id, attempt, reference):
    filename, digest, size = reference
    if record_id(filename) != identity:
        raise ValueError('Paid-description file does not match its reservation')
    changed = conn.execute("UPDATE description_file_writes SET file_status='ready' WHERE id=? "
        "AND request_id=? AND attempt_number=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (identity, request_id, attempt, digest, size)).rowcount
    if changed != 1:
        raise ValueError('Paid-description file reservation changed before completion')
