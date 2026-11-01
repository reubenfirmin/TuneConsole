"""Explicit naming boundary for installed pre-v103 storage.

Current owners have one physical name. Explicit writer identifiers and read
projections keep installed predecessors authoritative during preparation.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Owner:
    table: str
    columns: dict

    def column(self, name):
        return self.columns.get(name, name)


def owner(conn, name):
    """Name a physical writer explicitly, bypassing predecessor TEMP read views.

    Only SQL identifiers come from this finite historical vocabulary. Values,
    recovery documents and SQL statements are never rewritten. Callers retain
    their own transactions, UPSERTs, row counts and inserted row identities.
    #626 tracks removal after retained layouts and their writers are retired.
    """
    from .migrations.v103_entity_names import COLUMNS

    if name not in COLUMNS and name != 'accounts':
        raise ValueError('Unsupported account naming owner: ' + name)
    modern = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone()
    physical = 'identities' if name == 'accounts' and not modern else name
    columns = {} if modern else {new: old for old, new in COLUMNS.get(name, {}).items()}
    return Owner('main.' + physical, columns)


def accounts(conn):
    return 'accounts' if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='accounts' UNION ALL "
                                    "SELECT 1 FROM sqlite_temp_schema WHERE name='accounts'").fetchone() else 'identities'


def scope_columns(conn):
    return ('id','provider_playlist_id','account_id') if accounts(conn)=='accounts' else ('ref','provider_id','identity_id')


def membership_columns(conn):
    return ('playlist_scope_id','account_id') if accounts(conn)=='accounts' else ('scope_ref','identity_id')


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
    from .migrations.v103_entity_names import COLUMNS, COMMON
    mappings = {'identities':{},**COLUMNS,'quarantined_playlists':{'ytm':'playlist_scope_id'}}
    for name, in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='view'"):
        mappings.setdefault(name,COMMON)
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
            project(table,target,COLUMNS[table])
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recording_listening_artists'").fetchone():
        from .artist_credits import projection_table
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS recording_listening_artists AS '
            'SELECT DISTINCT e.recording_id,coalesce(r.artist_id,c.artist_id) artist_id '
            'FROM main.catalog_entries e JOIN main.'+projection_table(conn)+' c ON c.entry_id=e.id '
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
    from .preparation import subject_publication
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
