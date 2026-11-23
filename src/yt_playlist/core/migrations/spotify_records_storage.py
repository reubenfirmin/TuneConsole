"""Keep Spotify import and transfer records under their existing settings owners."""
import json

from .. import collection_recovery_epoch, spotify_job_state, spotify_records, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'own_spotify_import_and_transfer_records'
RETIRED = tuple(spotify_records.ARCHIVE_PREFIXES)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        "AND tbl_name IN (?,?) AND name NOT GLOB 'collection_recovery_changed_*' ORDER BY type,name", RETIRED)]


def install(conn, _context):
    for record in conn.execute('SELECT * FROM spotify_import_jobs ORDER BY id'):
        row = dict(record)
        identity = row['id']
        if row.pop('is_current'):
            worker_settings.save(conn, spotify_job_state.CURRENT, str(identity))
        spotify_records.save(conn, spotify_records.IMPORT + str(identity), row)
        spotify_records.note_identity(conn, spotify_records.IMPORT, identity)
    for record in conn.execute('SELECT * FROM spotify_transfer_receipts ORDER BY id'):
        row = dict(record)
        key = spotify_records.transfer_key(row['source_kind'], row['captured_account_id'], row['fingerprint'])
        row['slots'] = json.loads(row.pop('source_video_ids'))
        spotify_records.save(conn, key, row)
        spotify_records.note_identity(conn, spotify_records.TRANSFER, row['id'])
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    collection_recovery_epoch.install(conn)
    conn.execute('UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0')


def validate(conn):
    if definitions(conn, RETIRED):
        raise MigrationError('Incomplete Spotify workflow ownership cutover')
