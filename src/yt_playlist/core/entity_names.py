"""Historical account documents and retained predecessor read projections."""


# Read projections retain their own vocabulary independently of frozen DDL.
# Their separate admission-floor cleanup is tracked in #620.
PREDECESSOR_NAMES = {
    'identities': 'accounts', 'identity_id': 'account_id', 'scope_ref': 'playlist_scope_id',
    'ytm_playlist_id': 'provider_playlist_id', 'playlist_ref': 'playlist_scope_id',
    'ix_playlist_scope': 'ix_playlist_scope_account',
}
PREDECESSOR_COLUMNS = {
    'playlists': {'identity_id': 'account_id', 'scope_ref': 'playlist_scope_id',
                  'ytm_playlist_id': 'provider_playlist_id'},
    'playlist_scopes': {'identity_id': 'account_id', 'ref': 'id', 'provider_id': 'provider_playlist_id'},
    'playlist_membership_refs': {'identity_id': 'account_id', 'scope_ref': 'playlist_scope_id'},
    'playlist_library_revisions': {'identity_id': 'account_id', 'scope_ref': 'playlist_scope_id'},
    'item_ratings': {'identity_id': 'account_id'},
    'listening_calendars': {'identity_id': 'account_id'},
    'listening_capture_receipts': {'identity_id': 'account_id'},
    'listening_observations': {'identity_id': 'account_id'},
    'listening_rating_actions': {'identity_id': 'account_id'},
    'play_events': {'identity_id': 'account_id'},
    'player_events': {'identity_id': 'account_id'},
    'spotify_history_listens': {'identity_id': 'account_id'},
    'takeout_imports': {'identity_id': 'account_id'},
    'youtube_playback_alternates': {'identity_id': 'account_id'},
    'cleanup_ignored': {'ytm': 'playlist_scope_id'},
    'ignored_merge_members': {'playlist_ref': 'playlist_scope_id'},
    'suppressed_overlaps': {'a': 'first_playlist_scope_id', 'b': 'second_playlist_scope_id'},
    'overlap_kept': {'a': 'first_playlist_scope_id', 'b': 'second_playlist_scope_id'},
    'rec_mode_picks': {'playlist_id': 'membership_ref_id'},
}

def account_document(value):
    """Decode a historical account field without rewriting its retained document."""
    if isinstance(value,dict) and 'identity_id' in value:
        if 'account_id' in value:
            raise ValueError('Account document supplies competing account fields')
        return {('account_id' if key=='identity_id' else key):item for key,item in value.items()}
    return value


def external_account_document(value):
    return {('identity_id' if key=='account_id' else key):item for key,item in value.items()} if isinstance(value,dict) else value


def predecessor_reads(conn):
    """Project installed names while the predecessor remains authoritative.

    These connection-local views have no write triggers and disappear on close.
    Their explicit main references retain the pinned predecessor read snapshot.
    No persisted owner or installed definition is changed during admission.
    """
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone():
        return
    mappings = {'identities':{},**PREDECESSOR_COLUMNS,'quarantined_playlists':{'ytm':'playlist_scope_id'}}
    for name, in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='view'"):
        mappings.setdefault(name,PREDECESSOR_NAMES)
    installed = []
    quote = lambda name:'"'+name.replace('"','""')+'"'
    def project(table,target,mapping):
        columns = list(conn.execute('PRAGMA main.table_info('+quote(table)+')'))
        if not columns:
            return
        if target==table and not any(row[1] in mapping for row in columns):
            return
        projection = ','.join(quote(row[1])+' AS '+quote(mapping.get(row[1],row[1])) for row in columns)
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS '+quote(target)+' AS SELECT '+projection+' FROM main.'+quote(table))
        installed.append(target)
    for table,mapping in mappings.items():
        project(table,'accounts' if table=='identities' else table,mapping)
    # #620: retire these subject projections when deferred admission excludes v99.
    for table,target in (('play_events','play_event_subjects'),
                         ('listening_observations','listening_observation_subjects')):
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(target,)).fetchone():
            project(table,target,PREDECESSOR_COLUMNS[table])
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recording_listening_artists'").fetchone():
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS recording_listening_artists AS '
            'SELECT DISTINCT e.recording_id,coalesce(r.artist_id,c.artist_id) artist_id '
            'FROM main.catalog_entries e JOIN main.track_artists c ON c.entry_id=e.id '
            'LEFT JOIN main.artist_redirects r ON r.retired_id=c.artist_id')
        installed.append('recording_listening_artists')
    conn._entity_read_views = installed
    refresh_listening_reads(conn)


def refresh_listening_reads(conn):
    """Bind the explicit listening read family after additive publication DDL.

    The schema cookie belongs to this connection's snapshot. A reader pinned
    before installation keeps that snapshot; an idle preopened writer discovers
    the new read views without reopening or changing any physical capture owner.
    Selection and withdrawal happen inside those views, not in this lookup.
    """
    version = conn.execute('PRAGMA main.schema_version').fetchone()[0]
    if getattr(conn, '_listening_read_schema_version', None) == version:
        return
    from . import listening_subject_reads as subject_publication
    if subject_publication.native(conn) or not conn.execute(
            'SELECT 1 FROM main.sqlite_schema WHERE name=?', (subject_publication.table('head'),)).fetchone():
        if not conn.in_transaction:
            conn._listening_read_schema_version = version
        return
    transactional = conn.in_transaction
    installed = list(getattr(conn, '_entity_read_views', ()))
    for name in subject_publication.READS:
        existing = conn.execute('SELECT type FROM sqlite_temp_schema WHERE name=?', (name,)).fetchone()
        if existing and existing[0] == 'table':
            # A maintenance reader can own a private calculation buffer.
            continue
        conn.execute('DROP VIEW IF EXISTS temp.'+name)
        conn.execute('CREATE TEMP VIEW '+name+' AS SELECT * FROM main.'+subject_publication.table('read_'+name))
        if name not in installed:
            installed.append(name)
    conn._entity_read_views = installed
    if not transactional:
        conn._listening_read_schema_version = version


def clear_predecessor_reads(conn):
    for table in getattr(conn,'_entity_read_views',()):
        conn.execute('DROP VIEW IF EXISTS temp."'+table.replace('"','""')+'"')
    conn._entity_read_views = []
    conn._listening_read_schema_version = None
