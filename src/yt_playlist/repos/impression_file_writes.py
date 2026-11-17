"""Committed diagnostic identities before their serving facts are admitted."""
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE impression_file_writes (
    impression_id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    rank INTEGER NOT NULL,
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    actual = definitions(conn, ('impression_file_writes',))
    if not actual:
        conn.execute(SQL)
    elif tokens(actual['impression_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible served diagnostic file identities')


def validate(conn):
    actual = definitions(conn, ('impression_file_writes',))
    if not actual or tokens(actual['impression_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible served diagnostic file identities')


def reserve(conn, identity, request, surface, rank):
    conn.execute("INSERT INTO impression_file_writes VALUES (?,?,?,?,'pending')", (identity, request, surface, rank))


def complete(conn, identity, request, surface, rank):
    changed = conn.execute("UPDATE impression_file_writes SET file_status='ready' WHERE impression_id=? "
        "AND request_id=? AND surface=? AND rank=? AND file_status='pending'", (identity, request, surface, rank)).rowcount
    if changed != 1:
        raise ValueError('Served diagnostic differs from its pending owner')
