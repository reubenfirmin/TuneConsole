"""Indexed recommendation chunks owned by their ready file receipts."""
from . import worker_settings
from .db_revisions import keyed_reads
from .recommendation_content import FILE_IDENTITY, FILE_KIND

OWNER = 'prepared_outputs'
VIEW = 'recommendation_chunks'
RECEIPTS = 'collection_file_writes'
SERIAL = 'recommendation_chunk_id_high_water'
LEGACY_FILE_KIND = 'prepared-output'
SURFACE = "json_extract(owner_id,'$[2]')"
KEY = "json_extract(owner_id,'$[3]')"
ATTACHMENT = ('chunk_id', 'chunk_generation_id', 'chunk_filename', 'chunk_built_at',
              'chunk_expires_at', 'chunk_pool_id', 'chunk_first_rank', 'chunk_last_rank')
ASSIGN = ','.join(f'{column}=?' for column in ATTACHMENT)


def generation_reads(conn, generations):
    return keyed_reads(conn, RECEIPTS, generations, column='chunk_generation_id')


def attach(conn, file_identity, generation, filename, built_at, expires_at, pool, first, last):
    identity = int(worker_settings.read(conn, SERIAL, '0')) + 1
    changed = conn.execute(f'''UPDATE {RECEIPTS} SET {ASSIGN}
        WHERE kind='{FILE_KIND}' AND {FILE_IDENTITY}=? AND owner_id->>1=?
          AND file_status='ready' AND chunk_id IS NULL''',
        (identity, generation, filename, built_at, expires_at, pool, first, last, file_identity, OWNER)).rowcount
    if changed != 1:
        raise ValueError('Recommendation chunk requires its completed file receipt')
    worker_settings.save(conn, SERIAL, str(identity))


def detach(conn, generation, limit):
    ids = tuple(row[0] for row in conn.execute(
        f'SELECT id FROM {RECEIPTS} WHERE chunk_generation_id=? AND chunk_id IS NOT NULL LIMIT ?', (generation, limit)))
    if not ids:
        return 0
    marks = ','.join('?' for _ in ids)
    conn.execute(f"DELETE FROM {RECEIPTS} WHERE id IN ({marks}) AND kind=?", (*ids, LEGACY_FILE_KIND))
    conn.execute(f'UPDATE {RECEIPTS} SET ' + ','.join(f'{column}=NULL' for column in ATTACHMENT)
                 + f' WHERE id IN ({marks})', ids)
    return len(ids)


def relocate(conn, identity, filename):
    conn.execute(f'UPDATE {RECEIPTS} SET chunk_filename=? WHERE chunk_id=?', (filename, identity))
