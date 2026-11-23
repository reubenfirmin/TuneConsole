"""Move library sync progress to its account, playlist and settings owners."""
from collections import defaultdict

from .. import collection_recovery_epoch, sync_state, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'own_sync_progress_with_accounts_and_playlists'
RETIRED = ('sync_cursors',)
OWNERS = ('accounts', 'playlist_scopes')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND tbl_name IN (?,?,?) ORDER BY type,name', (*RETIRED, *OWNERS))]


def install(conn, _context):
    for table in OWNERS:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN ' + sync_state.COLUMN)
    row = conn.execute('SELECT * FROM sync_cursors WHERE id=0').fetchone()
    if row is not None:
        attempt = dict(row)
        for key in sync_state.HISTORY_FIELDS:
            value = attempt.pop(key)
            if value is not None:
                worker_settings.save(conn, key, str(value))
        sync_state.save_head(conn, attempt)
    groups = defaultdict(dict)
    for record in conn.execute('SELECT * FROM sync_cursors WHERE id>0 ORDER BY id'):
        row = dict(record)
        owner, key = sync_state.cursor_owner(row['account_id'], row['playlist_scope_id'], row['collection'])
        groups[owner][key] = row
    for owner, rows in groups.items():
        sync_state.save_resources(conn, owner, rows)
    conn.execute('DROP TABLE sync_cursors')
    collection_recovery_epoch.install(conn)
    conn.execute('UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0')


def validate(conn):
    installed = definitions(conn, (*RETIRED, *OWNERS))
    if set(installed) != set(OWNERS) or any(sync_state.COLUMN not in installed[owner] for owner in OWNERS):
        raise MigrationError('Incomplete library sync ownership cutover')
