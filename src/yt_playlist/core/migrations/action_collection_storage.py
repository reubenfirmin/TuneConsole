"""Keep ordered backup references and cleanup attempts on existing owners."""
from itertools import groupby

from .. import action_backup_inventory, collection_recovery, collection_recovery_epoch, playlist_cleanup_state
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from . import prediction_storage


NAME = 'own_action_backups_and_playlist_cleanup_attempts'
RETIRED = ('action_backups', 'playlist_gc_attempts')
OWNERS = ('actions', 'playlist_scopes', *RETIRED)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute("""SELECT sql FROM sqlite_schema
        WHERE tbl_name IN (?,?,?,?) AND sql IS NOT NULL ORDER BY type,name""", OWNERS)]


def _unresolved_scope(conn, remote):
    row = conn.execute("SELECT id FROM playlist_scopes WHERE provider_playlist_id=? AND kind='legacy'",
                       (remote,)).fetchone()
    if row:
        return row[0]
    scope = '@legacy:cleanup:' + remote
    conn.execute("INSERT INTO playlist_scopes(id,provider_playlist_id,kind) VALUES (?,?,'legacy')", (scope, remote))
    return scope


def _copy_cleanup_attempts(conn):
    for row in conn.execute('SELECT * FROM playlist_gc_attempts ORDER BY id'):
        account, remote = row['captured_account_id'], row['provider_playlist_id_snapshot']
        scope = row['playlist_scope_id'] or _unresolved_scope(conn, remote)
        value = collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                         kind='playlist-gc', owner=row['id'])
        # Before cutover these outcome fields were authoritative in SQLite.
        for key in ('attempts', 'last_attempt_at', 'next_retry_at', 'phase', 'failure_class', 'error'):
            if key in value or key in ('attempts', 'last_attempt_at', 'next_retry_at'):
                value[key] = row[key]
        playlist_cleanup_state.put(conn, scope, account, remote, value)


def install(conn, context):
    conn.execute('ALTER TABLE actions ADD COLUMN ' + action_backup_inventory.DECLARATION)
    conn.execute('ALTER TABLE playlist_scopes ADD COLUMN ' + playlist_cleanup_state.DECLARATION)
    backups = conn.execute('SELECT * FROM action_backups ORDER BY action_id,position')
    for action, items in groupby(backups, key=lambda row: row['action_id']):
        conn.execute('UPDATE actions SET backup_inventory=? WHERE id=?', (action_backup_inventory.encode(items), action))
    _copy_cleanup_attempts(conn)
    conn.execute('UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0 AND '
                 'EXISTS(SELECT 1 FROM playlist_gc_attempts)')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    prediction_storage.install_receipt_lookup(conn, context)
    conn.execute("DELETE FROM collection_file_writes WHERE kind='playlist-gc'")
    conn.execute(f'CREATE INDEX {playlist_cleanup_state.INDEX} ON playlist_scopes(id) WHERE {playlist_cleanup_state.PRESENT}')
    collection_recovery_epoch.install(conn)
    collection_recovery_epoch.install_keys(conn)


def validate(conn):
    layout = definitions(conn, (*OWNERS, playlist_cleanup_state.INDEX))
    if (any(name in layout for name in RETIRED) or playlist_cleanup_state.INDEX not in layout
            or action_backup_inventory.COLUMN not in layout.get('actions', '')
            or playlist_cleanup_state.COLUMN not in layout.get('playlist_scopes', '')):
        raise MigrationError('Incomplete action backup and playlist cleanup storage')
    collection_recovery_epoch.validate(conn)
    collection_recovery_epoch.validate_keys(conn)
