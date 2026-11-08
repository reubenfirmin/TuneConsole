"""Name account, scope and retained-membership references for their actual owners."""
import re


TOKENS = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                    r"`(?:``|[^`])*`|\[[^]]*\]|[A-Za-z_]\w*|\S",re.S)
COMMON = {'identities':'accounts','identity_id':'account_id','scope_ref':'playlist_scope_id',
          'ytm_playlist_id':'provider_playlist_id','playlist_ref':'playlist_scope_id',
          'ix_playlist_scope':'ix_playlist_scope_account'}
ACCOUNT_TABLES = ('playlists','playlist_scopes','playlist_membership_refs','playlist_library_revisions',
    'item_ratings','listening_calendars','listening_capture_receipts','listening_observations',
    'listening_rating_actions','play_events','player_events','spotify_history_listens',
    'takeout_imports','youtube_playback_alternates')
COLUMNS = {table:{'identity_id':'account_id'} for table in ACCOUNT_TABLES}
for _table in ('playlists','playlist_membership_refs','playlist_library_revisions'):
    COLUMNS[_table]['scope_ref'] = 'playlist_scope_id'
COLUMNS['playlists']['ytm_playlist_id'] = 'provider_playlist_id'
COLUMNS['playlist_scopes'].update(ref='id',provider_id='provider_playlist_id')
COLUMNS['cleanup_ignored'] = {'ytm':'playlist_scope_id'}
COLUMNS['ignored_merge_members'] = {'playlist_ref':'playlist_scope_id'}
for _table in ('suppressed_overlaps','overlap_kept'):
    COLUMNS[_table] = {'a':'first_playlist_scope_id','b':'second_playlist_scope_id'}
COLUMNS['rec_mode_picks'] = {'playlist_id':'membership_ref_id'}


def transform(sql):
    """Transform SQL identifiers, preserving literal messages and historical formats."""
    tokens = [match.group() for match in TOKENS.finditer(sql)]
    identifiers = {token.strip('"`[]').lower() for token in tokens
                   if not token.startswith(("'",'--','/*'))}
    names = dict(COMMON)
    if 'playlist_scopes' in identifiers:
        names.update(ref='id',provider_id='provider_playlist_id')
    if identifiers & {'cleanup_ignored','quarantined_playlists'}:
        names['ytm'] = 'playlist_scope_id'
    if identifiers & {'suppressed_overlaps','overlap_kept'}:
        names.update(a='first_playlist_scope_id',b='second_playlist_scope_id')
    if 'rec_mode_picks' in identifiers:
        names['playlist_id'] = 'membership_ref_id'

    def replace(match):
        token = match.group()
        if token.startswith(("'",'--','/*')):
            return token
        quoted = token.startswith(('"','`','['))
        key = token[1:-1] if quoted else token
        value = names.get(key.lower())
        return token if value is None else token[0]+value+token[-1] if quoted else value

    return TOKENS.sub(replace,sql)


def apply(conn,context):
    # Drop dependent programs before column renames, then publish the exact
    # canonical fresh definitions. ALTER preserves rows, IDs and FK actions.
    from .. import group_retention_progress
    retention_progress = group_retention_progress.available(conn)
    if retention_progress:
        group_retention_progress.validate(conn)
    dependent = list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    for kind,name,_ in reversed(dependent):
        conn.execute('DROP '+kind+' "'+name.replace('"','""')+'"')
    conn.execute('ALTER TABLE identities RENAME TO accounts')
    for table,columns in COLUMNS.items():
        for old,new in columns.items():
            conn.execute(f'ALTER TABLE {table} RENAME COLUMN {old} TO {new}')
    conn.execute('DROP INDEX ix_playlist_scope')
    conn.execute('CREATE INDEX ix_playlist_scope_account ON playlists(playlist_scope_id,account_id)')
    restore_programs(conn, dependent, retention_progress=retention_progress)


def restore_programs(conn, dependent, *, retention_progress):
    """Restore canonical programs and retained custom programs after renaming."""
    from .. import group_retention_progress
    from .current import statements
    canonical = set()
    for sql in statements(version=103):
        if re.match(r'CREATE (?:VIEW|TRIGGER)\b',sql.lstrip(),re.I):
            if retention_progress and sql.startswith('CREATE TRIGGER retained_group_publication_delete '):
                sql = group_retention_progress.publication_guard()
            conn.execute(sql)
            canonical.add(sql.split()[2].strip('"`[]'))
    for _,name,sql in dependent:
        if name not in canonical:
            conn.execute(transform(sql))
