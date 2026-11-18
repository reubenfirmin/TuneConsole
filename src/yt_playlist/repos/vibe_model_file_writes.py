"""Pending and ready shared-model file IDs, independent of model digests."""
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE vibe_model_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation BLOB NOT NULL CHECK(typeof(generation)='blob' AND length(generation)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 16777216),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
INDEX = 'CREATE INDEX ix_vibe_model_file_generation ON vibe_model_file_writes(generation,file_status,id)'


def install(conn, context=None):
    actual = definitions(conn, ('vibe_model_file_writes', 'ix_vibe_model_file_generation'))
    for name, sql in (('vibe_model_file_writes', SQL), ('ix_vibe_model_file_generation', INDEX)):
        if name not in actual:
            conn.execute(sql)
        elif tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible shared-model file owner: ' + name)


def validate(conn):
    actual = definitions(conn, ('vibe_model_file_writes', 'ix_vibe_model_file_generation'))
    for name, sql in (('vibe_model_file_writes', SQL), ('ix_vibe_model_file_generation', INDEX)):
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible shared-model file owner: ' + name)


def reserve(conn, generation, size):
    install(conn)
    return conn.execute("INSERT INTO vibe_model_file_writes(generation,byte_size,file_status) VALUES (?,?,'pending')",
                        (bytes.fromhex(generation), size)).lastrowid


def complete(conn, identity, generation, size):
    changed = conn.execute("UPDATE vibe_model_file_writes SET file_status='ready' WHERE id=? AND generation=? "
        "AND byte_size=? AND file_status='pending'", (identity, bytes.fromhex(generation), size)).rowcount
    if changed != 1:
        raise ValueError('Shared-model file reservation changed before completion')


def selected(conn, generation):
    if 'vibe_model_file_writes' not in definitions(conn, ('vibe_model_file_writes',)):
        return None
    row = conn.execute("SELECT id FROM vibe_model_file_writes WHERE generation=? AND file_status='ready' "
                       'ORDER BY id DESC LIMIT 1', (bytes.fromhex(generation),)).fetchone()
    return row[0] if row else None
