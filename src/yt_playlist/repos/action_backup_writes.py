"""SQL ownership of retained playlist backup files; callers own transactions."""
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE action_backup_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL CHECK(action_id>0),
    position INTEGER NOT NULL CHECK(position>=0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 67108864),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='action_backup_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible action backup file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='action_backup_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible action backup file identities')


def reserve(conn, action, position, integrity):
    install(conn)
    return conn.execute('INSERT INTO action_backup_writes(action_id,position,content_digest,byte_size,file_status) '
                        "VALUES (?,?,?,?,'pending')", (action, position, *integrity)).lastrowid


def complete(conn, identity, action, position, reference):
    changed = conn.execute("UPDATE action_backup_writes SET file_status='ready' "
        "WHERE id=? AND action_id=? AND position=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (identity, action, position, *reference[1:])).rowcount
    if changed != 1:
        raise ValueError('Action backup reservation changed before completion')
