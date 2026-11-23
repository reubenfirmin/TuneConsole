"""Fold account attributes and retained playlist membership into their owners."""
import re

from .. import group_preview_work
from ..schema_contract import MigrationError, tokens
from .track_source_cutover import _copy_table


NAME = 'fold_account_and_playlist_storage'
ACCOUNT_TABLES = ('account_auth_expirations', 'listening_calendars', 'account_album_state')
RETIRED_TABLES = (*ACCOUNT_TABLES, 'playlist_membership_refs')
REFERENCE_TABLES = ('action_targets', 'play_events', 'rec_mode_picks', 'tv_continuation_receipts')
FK_OWNERS = (*RETIRED_TABLES, 'playlists', *REFERENCE_TABLES)
RETIRED_GUARDS = frozenset((
    'playlists_membership_insert', 'playlists_membership_record',
    'playlist_membership_refs_delete', 'playlist_membership_refs_update',
    'owned_playlist_membership_refs_insert', 'owned_playlist_membership_refs_update',
    *(f'listening_summary_listening_calendars_{event}' for event in ('insert', 'update', 'delete')),
))
ACCOUNT_COLUMNS = (
    'auth_expired INTEGER NOT NULL DEFAULT 0 CHECK(auth_expired IN (0,1))',
    'listening_timezone TEXT',
    'listening_timezone_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(listening_timezone_confirmed IN (0,1) '
    'AND (listening_timezone IS NOT NULL OR listening_timezone_confirmed=0))',
    "saved_album_revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(saved_album_revision)='integer' "
    'AND saved_album_revision>=0)',
    "saved_album_synced_at REAL CHECK(saved_album_synced_at IS NULL OR (typeof(saved_album_synced_at) IN ('integer','real') "
    'AND abs(saved_album_synced_at)<=1.7976931348623157e308))',
)
ITEM_TABLES = ('playlist_items', 'unresolved_playlist_items')
CURRENT_INDEX = 'uq_playlists_current_account_provider'
LOOKUP_INDEX = 'ix_playlists_account_provider'


def predecessor_contract(conn, baseline):
    """The installed layout immediately before this change is still admissible."""
    names = set(RETIRED_TABLES) | RETIRED_GUARDS | {'accounts', 'playlists', *REFERENCE_TABLES}
    names.update(name for name in baseline if name.startswith((
        'action_target_agreement_', 'play_event_membership_', 'playlist_revision_',
        'playlists_membership_', 'playlists_scope_', 'rec_capture_playlists_', 'rec_evidence_playlists_')))
    prior = {name: baseline[name] for name in names}
    # The numbered layout predates these guards.
    prior.update((name, ('trigger', tokens(sql))) for name, sql in
                 group_preview_work.guards(conn, sources={'accounts', 'playlists'}))
    return prior


def _fold_accounts(conn):
    for column in ACCOUNT_COLUMNS:
        conn.execute('ALTER TABLE accounts ADD COLUMN ' + column)
    conn.execute('UPDATE accounts SET auth_expired=1 WHERE id IN (SELECT account_id FROM account_auth_expirations)')
    conn.execute('UPDATE accounts SET (listening_timezone,listening_timezone_confirmed)='
                 '(SELECT timezone,confirmed FROM listening_calendars WHERE account_id=accounts.id) '
                 'WHERE id IN (SELECT account_id FROM listening_calendars)')
    # When the unattributed set is present, use only observations of that set.
    pending_attribution = conn.execute('SELECT 1 FROM unattributed_album_memberships LIMIT 1').fetchone()
    observed = 'legacy_cohort_observed_at' if pending_attribution else 'snapshot_observed_at'
    conn.execute('UPDATE accounts SET (saved_album_revision,saved_album_synced_at)='
                 f'(SELECT revision,{observed} FROM account_album_state WHERE account_id=accounts.id) '
                 'WHERE id IN (SELECT account_id FROM account_album_state)')
    for event, when in (
        ('INSERT', 'NEW.listening_timezone IS NOT NULL'),
        ('UPDATE OF listening_timezone,listening_timezone_confirmed',
         'OLD.listening_timezone IS NOT NEW.listening_timezone OR '
         'OLD.listening_timezone_confirmed IS NOT NEW.listening_timezone_confirmed'),
        ('DELETE', 'OLD.listening_timezone IS NOT NULL'),
    ):
        name = event.split()[0].lower()
        conn.execute(f'CREATE TRIGGER listening_summary_accounts_calendar_{name} AFTER {event} ON accounts '
                     f'WHEN {when} BEGIN INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
                     'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')


def _sequence(conn, table):
    row = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
    return row[0] if row else None


def _preserve_sequence(conn, table, previous):
    if previous is None:
        return
    if _sequence(conn, table) is None:
        conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (table, previous))
    else:
        conn.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (previous, table))


def _playlist_sql(sql):
    return (sql.replace('id INTEGER PRIMARY KEY,', 'id INTEGER PRIMARY KEY AUTOINCREMENT,', 1)
            .replace('account_id INTEGER NOT NULL', 'account_id INTEGER', 1)
            .replace('provider_playlist_id TEXT NOT NULL', 'provider_playlist_id TEXT', 1)
            .replace('UNIQUE(account_id, provider_playlist_id)',
                     'is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),\n'
                     '  CHECK((account_id IS NULL)=(playlist_scope_id IS NULL) AND '
                     '(account_id IS NULL)=(provider_playlist_id IS NULL) AND '
                     '(is_current=0 OR account_id IS NOT NULL))', 1))


def _playlist_guard(name, sql):
    sql = sql.replace('playlist_membership_refs', 'playlists')
    if name in ('playlists_scope_insert', 'playlists_scope_update'):
        return sql.replace('WHEN NOT EXISTS', 'WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS', 1).replace(
            'AND ((s.kind=', "AND ((NEW.is_current=0 AND (s.kind<>'account' OR s.account_id=NEW.account_id)) OR (s.kind=", 1)
    if name == 'playlists_membership_update':
        return sql.replace(' BEGIN SELECT RAISE', ' OR NEW.provider_playlist_id IS NOT OLD.provider_playlist_id '
                           'OR NEW.is_current>OLD.is_current BEGIN SELECT RAISE', 1)
    if name in ('rec_capture_playlists_update', 'rec_evidence_playlists_update'):
        return sql.replace(' BEGIN ', ' OR OLD.is_current IS NOT NEW.is_current BEGIN ', 1)
    if name in ('rec_capture_playlists_insert', 'rec_evidence_playlists_insert'):
        return sql.replace(' BEGIN ', ' WHEN NEW.is_current=1 BEGIN ', 1)
    return sql


def _fold_playlists(conn):
    if conn.execute('SELECT 1 FROM playlists p LEFT JOIN playlist_membership_refs m ON m.id=p.id '
                    'WHERE m.id IS NULL OR m.account_id IS NOT p.account_id '
                    'OR m.playlist_scope_id IS NOT p.playlist_scope_id LIMIT 1').fetchone():
        raise MigrationError('Current playlist does not match its retained membership')
    playlist_sql, = conn.execute("SELECT sql FROM sqlite_schema WHERE name='playlists' AND type='table'").fetchone()
    affected = ('playlists', 'playlist_membership_refs')
    definitions = [row for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "type IN ('view','index','trigger') ORDER BY type,name")
        if row[2] in affected or row[0] in ('view', 'trigger')
        and re.search(r'\b(?:playlists|playlist_membership_refs)\b', row[3])]
    current_sequence = _sequence(conn, 'playlists')
    retained_sequence = _sequence(conn, 'playlist_membership_refs')
    staging = _copy_table(conn, 'playlists', _playlist_sql(playlist_sql), expressions={'is_current': '1'})
    conn.execute(f'INSERT INTO "{staging}"(id,account_id,playlist_scope_id,provider_playlist_id,is_current) '
                 'SELECT m.id,m.account_id,m.playlist_scope_id,s.provider_playlist_id,0 '
                 'FROM playlist_membership_refs m LEFT JOIN playlist_scopes s ON s.id=m.playlist_scope_id '
                 'WHERE NOT EXISTS(SELECT 1 FROM playlists p WHERE p.id=m.id)')
    for kind, name, table, _ in definitions:
        if kind in ('view', 'trigger') and table != 'playlists':
            conn.execute(f'DROP {kind} "{name}"')
    conn.execute('DROP TABLE playlists')
    # Modern SQLite updates incoming references in their existing definitions.
    # Switch back to the runner's legacy rename mode for the normal table swap.
    conn.execute('PRAGMA legacy_alter_table=OFF')
    try:
        conn.execute('ALTER TABLE playlist_membership_refs RENAME TO playlists')
    finally:
        conn.execute('PRAGMA legacy_alter_table=ON')
    conn.execute('DROP TABLE playlists')
    conn.execute(f'ALTER TABLE "{staging}" RENAME TO playlists')
    _preserve_sequence(conn, 'playlists', current_sequence)
    _preserve_sequence(conn, 'playlists', retained_sequence)
    for kind in ('view', 'index', 'trigger'):
        for definition_kind, name, table, sql in definitions:
            if definition_kind == kind and name not in RETIRED_GUARDS and table != 'playlist_membership_refs':
                conn.execute(_playlist_guard(name, sql))
    conn.execute(f'CREATE UNIQUE INDEX {CURRENT_INDEX} ON playlists(account_id,provider_playlist_id) WHERE is_current=1')
    conn.execute(f'CREATE INDEX {LOOKUP_INDEX} ON playlists(account_id,provider_playlist_id,is_current)')
    conn.execute('CREATE VIEW active_playlists AS SELECT * FROM playlists WHERE is_current=1')
    for table in ITEM_TABLES:
        for event in ('INSERT', 'UPDATE OF playlist_id'):
            name = event.split()[0].lower()
            conn.execute(f'CREATE TRIGGER {table}_active_owner_{name} BEFORE {event} ON {table} '
                         'WHEN NOT EXISTS(SELECT 1 FROM active_playlists WHERE id=NEW.playlist_id) '
                         "BEGIN SELECT RAISE(ABORT,'Playlist membership is no longer active'); END")
    conn.execute('CREATE TRIGGER playlists_retire_empty BEFORE UPDATE OF is_current ON playlists '
                 'WHEN OLD.is_current=1 AND NEW.is_current=0 AND ('
                 'EXISTS(SELECT 1 FROM playlist_items WHERE playlist_id=OLD.id) OR '
                 'EXISTS(SELECT 1 FROM unresolved_playlist_items WHERE playlist_id=OLD.id)) '
                 "BEGIN SELECT RAISE(ABORT,'Remove playlist tracks before retiring the membership'); END")


def install(conn, _context):
    _fold_accounts(conn)
    _fold_playlists(conn)
    for table in ACCOUNT_TABLES:
        conn.execute('DROP TABLE ' + table)
    group_preview_work.install_guards(conn, sources={'accounts', 'playlists'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def applied(conn):
    return conn.execute('SELECT 1 FROM schema_changes WHERE name=?', (NAME,)).fetchone() is not None


def validate(conn):
    from . import account_bindings

    names = {row[0] for row in conn.execute('SELECT name FROM sqlite_schema')}
    indexes = ((account_bindings.CURRENT_INDEX, account_bindings.LOOKUP_INDEX)
               if account_bindings.CURRENT_INDEX in names else (CURRENT_INDEX, LOOKUP_INDEX))
    if set(RETIRED_TABLES) & names or not {'active_playlists', *indexes} <= names:
        raise MigrationError('Account and playlist membership storage was not folded')
