"""Pending and ready shared-model file IDs, independent of model digests."""
from yt_playlist.core.vibe_model_files import FILE_KIND

_IDENTITY = 'CAST(owner_id AS INTEGER)'
_KIND = f"kind='{FILE_KIND}'"


def reserve(conn, generation, size):
    return conn.execute('INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) '
        f"SELECT ?,COALESCE(max({_IDENTITY}),0)+1,?,?,'pending' FROM collection_file_writes WHERE {_KIND} "
        f'RETURNING {_IDENTITY}', (FILE_KIND, bytes.fromhex(generation).hex(), size)).fetchone()[0]


def complete(conn, identity, generation, size):
    changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
        f"WHERE {_KIND} AND {_IDENTITY}=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (identity, bytes.fromhex(generation).hex(), size)).rowcount
    if changed != 1:
        raise ValueError('Shared-model file reservation changed before completion')


def selected(conn, generation):
    row = conn.execute(f'SELECT {_IDENTITY} FROM collection_file_writes '
        f"WHERE {_KIND} AND content_digest=? AND file_status='ready' ORDER BY {_IDENTITY} DESC LIMIT 1",
        (bytes.fromhex(generation).hex(),)).fetchone()
    return row[0] if row else None


def digest(conn, identity):
    row = conn.execute(f'SELECT content_digest FROM collection_file_writes WHERE {_KIND} AND {_IDENTITY}=?',
                       (identity,)).fetchone()
    return row[0] if row else None
