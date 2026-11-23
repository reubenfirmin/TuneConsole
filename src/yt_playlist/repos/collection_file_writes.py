"""SQL identities for immutable collection bodies, shared by their SQL owners.

Callers own a single repository transaction. This module performs no file access
and does not open, join or coordinate transactions.
"""
from yt_playlist.core.document_paths import record_id


def reserve(conn, kind, owner, integrity, *, identity=None):
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
