"""Remove account credentials and derive playlist provider IDs from their scope."""
from yt_playlist.core import group_preview_work
from yt_playlist.core.schema_contract import MigrationError
from . import account_storage
from .track_source_cutover import _replace_table


NAME = 'canonical_account_and_playlist_bindings'
CURRENT_INDEX = 'uq_playlists_current_account_scope'
LOOKUP_INDEX = 'ix_playlists_account_scope'
_RETIRED_INDEXES = (account_storage.CURRENT_INDEX, account_storage.LOOKUP_INDEX)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "(name IN ('accounts','active_playlists','group_preview_changed_accounts_update') OR tbl_name='playlists') "
        "ORDER BY type,name")]


def install(conn, _context):
    conn.execute('DROP TRIGGER group_preview_changed_accounts_update')
    conn.execute('ALTER TABLE accounts DROP COLUMN credential_ref')
    conn.execute('ALTER TABLE accounts DROP COLUMN last_auth_ok')

    if conn.execute('SELECT 1 FROM playlists p LEFT JOIN playlist_scopes s ON s.id=p.playlist_scope_id '
                    'WHERE p.provider_playlist_id IS NOT s.provider_playlist_id LIMIT 1').fetchone():
        raise MigrationError('Playlist provider ID disagrees with its scope')
    sequence = account_storage._sequence(conn, 'playlists')
    sql, = conn.execute("SELECT sql FROM sqlite_schema WHERE name='playlists'").fetchone()
    sql = sql.replace('  provider_playlist_id TEXT,\n', '').replace(
        '(account_id IS NULL)=(provider_playlist_id IS NULL) AND ', '')
    owners = conn.execute(
        "SELECT name,sql FROM sqlite_schema WHERE tbl_name='playlists' AND sql IS NOT NULL "
        "AND type IN ('index','trigger') ORDER BY type,name").fetchall()
    conn.execute('DROP VIEW active_playlists')
    _replace_table(conn, 'playlists', sql)
    account_storage._preserve_sequence(conn, 'playlists', sequence)
    for name, definition in owners:
        if name in _RETIRED_INDEXES or name.startswith('group_preview_changed_playlists_'):
            continue
        if name in ('playlists_scope_insert', 'playlists_scope_update'):
            definition = definition.replace(' AND s.provider_playlist_id=NEW.provider_playlist_id', '')
        if name == 'playlists_membership_update':
            definition = definition.replace(' OR NEW.provider_playlist_id IS NOT OLD.provider_playlist_id', '')
        conn.execute(definition)
    conn.execute(f'CREATE UNIQUE INDEX {CURRENT_INDEX} ON playlists(account_id,playlist_scope_id) WHERE is_current=1')
    conn.execute(f'CREATE INDEX {LOOKUP_INDEX} ON playlists(account_id,playlist_scope_id,is_current)')
    conn.execute('CREATE VIEW active_playlists AS SELECT p.*,s.provider_playlist_id FROM playlists p '
                 'JOIN playlist_scopes s ON s.id=p.playlist_scope_id WHERE p.is_current=1')
    group_preview_work.install_guards(conn, sources={'accounts', 'playlists'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate(conn):
    from ..schema_inspection import definitions

    objects = definitions(conn, (CURRENT_INDEX, LOOKUP_INDEX, *_RETIRED_INDEXES))
    if set(objects) != {CURRENT_INDEX, LOOKUP_INDEX}:
        raise MigrationError('Playlist scope is not the canonical provider binding')
