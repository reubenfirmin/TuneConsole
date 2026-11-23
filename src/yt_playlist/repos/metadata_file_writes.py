"""SQL file reservations and exact revision references for runtime metadata."""
from contextlib import nullcontext

from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core.metadata_file_contract import owner_key, revision_key


def reserve(conn, record):
    owner = record['owner']
    identity = owner[0] if type(owner[0]) is int else None
    return conn.execute('INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) '
        "VALUES ('runtime-metadata',json_array(?,?,?,?),?,?,'pending')",
        (record['kind'], owner_key(record['kind'], owner), revision_key(record['revision']), identity,
         record['digest'], record['size'])).lastrowid


def complete(conn, record, *, kind, owner, revision):
    if (record['kind'], tuple(record['owner']), record['revision']) != (kind, tuple(owner), revision):
        raise ValueError('Runtime metadata file belongs to a different head')
    kind, owner, revision = record['kind'], owner_key(record['kind'], record['owner']), revision_key(record['revision'])
    changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
        "WHERE kind='runtime-metadata' AND file_address=? AND owner_id->>0=? "
        "AND metadata_owner_key=? AND owner_id->>2=? AND content_digest=? AND byte_size=? AND file_status='pending'",
        (record['file_id'], kind, owner, revision, record['digest'], record['size'])).rowcount
    if changed != 1:
        raise ValueError('Runtime metadata file reservation changed before completion')
    conn.execute("UPDATE collection_file_writes SET selected=0 WHERE kind='runtime-metadata' "
                 'AND owner_id->>0=? AND metadata_owner_key=? AND owner_id->>2=? AND selected=1', (kind, owner, revision))
    conn.execute("UPDATE collection_file_writes SET selected=1 WHERE kind='runtime-metadata' AND file_address=?",
                 (record['file_id'],))


def reference(conn, kind, owner, revision, *, store=None):
    if revision is None:
        return None
    key = owner_key(kind, owner)
    with keyed_reads(store, 'collection_file_writes', [key]) if store is not None else nullcontext():
        row = conn.execute('SELECT file_address,content_digest,byte_size FROM collection_file_writes '
                           "WHERE kind='runtime-metadata' AND owner_id->>0=? AND metadata_owner_key=? "
                           'AND owner_id->>2=? AND selected=1',
                           (kind, key, revision_key(revision))).fetchone()
    return tuple(row) if row else None


def retains(conn, identity):
    return conn.execute("SELECT 1 FROM collection_file_writes WHERE kind='runtime-metadata' "
                        'AND file_address=? AND selected=1', (identity,)).fetchone() is not None


def next_conflict_id(conn):
    following = conn.execute('SELECT max(COALESCE((SELECT max(id) FROM metadata_conflicts),0),'
        "COALESCE((SELECT seq FROM sqlite_sequence WHERE name='metadata_conflicts'),0))+1").fetchone()[0]
    pending = conn.execute("SELECT max(owner_id->>3) FROM collection_file_writes WHERE kind='runtime-metadata' "
                           "AND owner_id->>0='conflicts'").fetchone()[0]
    return max(following, (pending or 0) + 1)
