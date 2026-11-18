"""SQL identities for immutable collection bodies, shared by their SQL owners.

Callers own a single repository transaction. This module performs no file access
and does not open, join or coordinate transactions.
"""
from yt_playlist.core.document_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE collection_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 16777216),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='collection_file_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible collection document identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='collection_file_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible collection document identities')


def reserve(conn, kind, owner, integrity, *, identity=None):
    # Installed formats can keep serving while regular admission is deferred.
    # A new write admits only this empty SQL owner, inside its repository's
    # reservation transaction. It converts no old data or files and does not
    # advance regular history; change 40 later validates the same definition.
    install(conn)
    return conn.execute('INSERT INTO collection_file_writes(id,kind,owner_id,content_digest,byte_size,file_status) '
                        "VALUES (?,?,?,?,?,'pending')", (identity, kind, str(owner), *integrity)).lastrowid


def complete(conn, identity, kind, owner, reference):
    if record_id(reference[0]) != identity:
        raise ValueError('Collection document reservation changed before completion')
    changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
        "WHERE id=? AND kind=? AND owner_id=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (identity, kind, str(owner), *reference[1:])).rowcount
    if changed != 1:
        raise ValueError('Collection document reservation changed before completion')
