"""Put current-job selection and ordered transfer slots on their existing owners."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'consolidate_spotify_import_storage'
RETIRED = ('spotify_import_job_head', 'spotify_transfer_items')
MARKER = 'uq_spotify_import_job_current'
OWNERS = ('spotify_import_jobs', 'spotify_transfer_receipts', *RETIRED)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND tbl_name IN (?,?,?,?) AND name NOT GLOB \'collection_recovery_changed_*\' ORDER BY type,name', OWNERS)]


def install(conn, _context):
    conn.execute('ALTER TABLE spotify_import_jobs ADD COLUMN is_current INTEGER NOT NULL DEFAULT 0 '
                 'CHECK(is_current IN (0,1))')
    conn.execute('UPDATE spotify_import_jobs SET is_current=1 '
                 'WHERE id=(SELECT job_id FROM spotify_import_job_head WHERE id=1)')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON spotify_import_jobs(is_current) WHERE is_current=1')
    conn.execute("ALTER TABLE spotify_transfer_receipts ADD COLUMN source_video_ids TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(source_video_ids) IS 'array')")
    conn.execute('UPDATE spotify_transfer_receipts AS receipt SET source_video_ids=('
                 'SELECT json_group_array(video_id_snapshot ORDER BY position) '
                 'FROM spotify_transfer_items WHERE receipt_id=receipt.id)')
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)


def validate(conn):
    installed = definitions(conn, (*OWNERS, MARKER))
    if (set(installed) != {'spotify_import_jobs', 'spotify_transfer_receipts', MARKER}
            or 'source_video_ids' not in tokens(installed.get('spotify_transfer_receipts'))):
        raise MigrationError('Incomplete Spotify import storage consolidation')
