"""SQL identities for the finite recommendation output families.

The calling repository owns its transaction. These functions never access files
or open a transaction.
"""
from yt_playlist.core.document_paths import record_id
from yt_playlist.core.recommendation_chunks import OWNER as CHUNK_OWNER
from yt_playlist.core.recommendation_content import FILE_IDENTITY, FILE_KIND

OWNERS = frozenset({CHUNK_OWNER, 'listening_output_files', 'home_display_files',
                   'recommendation_report_files', 'radio_seed_output', 'model_autotune_output'})


def reserve(conn, owner, surface, chunk_key, integrity):
    if owner not in OWNERS:
        raise ValueError('Unknown recommendation file owner')
    return conn.execute('INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) '
        f"VALUES ('{FILE_KIND}',json_array(COALESCE((SELECT seq FROM sqlite_sequence "
        "WHERE name='collection_file_writes'),0)+1,?,?,?),?,?,'pending') "
        f'RETURNING {FILE_IDENTITY}', (owner, surface, chunk_key, *integrity)).fetchone()[0]


def complete(conn, identity, owner, surface, chunk_key, reference):
    if record_id(reference[0]) != identity:
        raise ValueError('Recommendation file reservation changed before completion')
    changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
        f"WHERE kind='{FILE_KIND}' AND {FILE_IDENTITY}=? AND owner_id=json_array(?,?,?,?) "
        "AND content_digest=? AND byte_size=? "
        "AND file_status='pending'", (identity, identity, owner, surface, chunk_key, *reference[1:])).rowcount
    if changed != 1:
        raise ValueError('Recommendation file reservation changed before completion')
