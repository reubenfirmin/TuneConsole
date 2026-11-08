"""Consolidate scope attributes and coherent account album checkpoints.

Runtime readers/writers and the current bootstrap share these owners. Retained
playlist lifetime identities are neither rebuilt nor deduplicated here.
"""
import math

from .runner import MigrationError


SCOPE_COLUMNS = (
    'group_name TEXT',
    "hidden INTEGER NOT NULL DEFAULT 0 CHECK(typeof(hidden)='integer' AND hidden IN (0,1))",
    "overlap_ignored INTEGER NOT NULL DEFAULT 0 CHECK(typeof(overlap_ignored)='integer' AND overlap_ignored IN (0,1))",
    'overlap_ignored_at REAL CHECK((overlap_ignored=1 OR overlap_ignored_at IS NULL) AND '
    "(overlap_ignored_at IS NULL OR (typeof(overlap_ignored_at) IN ('integer','real') AND "
    'overlap_ignored_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)))',
    "origin TEXT CHECK(origin IS NULL OR origin IN ('ordinary','tuneconsole'))",
    "adopted INTEGER CHECK((origin IS NULL AND adopted IS NULL) OR (origin IS NOT NULL "
    "AND typeof(adopted)='integer' AND adopted IN (0,1)))",
    'provenance_description TEXT CHECK(origin IS NOT NULL OR provenance_description IS NULL)',
)
SCOPE_TABLE = (
    'CREATE TABLE playlist_scopes (ref TEXT PRIMARY KEY NOT NULL,provider_id TEXT NOT NULL,'
    'identity_id INTEGER REFERENCES identities(id),kind TEXT NOT NULL '
    "CHECK(kind IN ('shared','account','legacy'))," + ','.join(SCOPE_COLUMNS) + ','
    "CHECK((kind='account' AND identity_id IS NOT NULL) OR (kind<>'account' AND identity_id IS NULL)),"
    'UNIQUE(provider_id,identity_id))'
)


def _clock(name):
    return (f"CHECK({name} IS NULL OR (typeof({name}) IN ('integer','real') AND "
            f'{name} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308))')


def statements():
    yield ('CREATE TABLE account_album_state (account_id INTEGER PRIMARY KEY NOT NULL REFERENCES identities(id),'
           "revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(revision)='integer' AND revision>=0),"
           'snapshot_observed_at REAL ' + _clock('snapshot_observed_at') + ','
           "legacy_cohort_present INTEGER NOT NULL DEFAULT 0 CHECK(typeof(legacy_cohort_present)='integer' "
           'AND legacy_cohort_present IN (0,1)),legacy_cohort_observed_at REAL '
           + _clock('legacy_cohort_observed_at') + ','
           'CHECK(legacy_cohort_present=1 OR legacy_cohort_observed_at IS NULL)) WITHOUT ROWID')
    yield ('CREATE TABLE account_albums (account_id INTEGER NOT NULL REFERENCES identities(id),'
           'album_id INTEGER NOT NULL REFERENCES albums(id),source TEXT NOT NULL,saved_at REAL '
           + _clock('saved_at') + ',PRIMARY KEY(account_id,album_id)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_account_album_release ON account_albums(album_id,account_id)'
    # Unknown authorship has its own relation rather than a nullable composite
    # primary key or an invented account. This is still a queried current set.
    yield ('CREATE TABLE unattributed_album_memberships (album_id INTEGER PRIMARY KEY NOT NULL REFERENCES albums(id),'
           'source TEXT NOT NULL,saved_at REAL ' + _clock('saved_at') + ') WITHOUT ROWID')
    yield ('CREATE VIEW saved_album_memberships AS SELECT account_id,album_id,source,saved_at FROM account_albums '
           'UNION ALL SELECT NULL,album_id,source,saved_at FROM unattributed_album_memberships')
    # ix_playlist_shared already enforces the NULL-account scope identity.
    yield 'CREATE INDEX ix_playlist_scope_group ON playlist_scopes(group_name) WHERE group_name IS NOT NULL'
    yield 'CREATE INDEX ix_playlist_scope_hidden ON playlist_scopes(ref) WHERE hidden=1'
    yield 'CREATE INDEX ix_playlist_scope_overlap_ignored ON playlist_scopes(ref) WHERE overlap_ignored=1'
    yield ("CREATE VIEW quarantined_playlists AS SELECT ref ytm,'Generated' name FROM playlist_scopes "
           "WHERE origin='tuneconsole' AND adopted=0")
    for table in ('account_albums', 'unattributed_album_memberships'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            condition = ' WHEN OLD.album_id IS NOT NEW.album_id' if event == 'UPDATE' else ''
            yield (f'CREATE TRIGGER rec_capture_{table}_{event.lower()} AFTER {event} ON {table}{condition} '
                   "BEGIN INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                   'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')
            yield (f'CREATE TRIGGER rec_evidence_{table}_{event.lower()} AFTER {event} ON {table}{condition} '
                   'BEGIN UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        if event == 'UPDATE':
            condition = ('OLD.group_name IS NOT NEW.group_name OR OLD.origin IS NOT NEW.origin '
                         'OR OLD.adopted IS NOT NEW.adopted')
        else:
            prefix = 'OLD' if event == 'DELETE' else 'NEW'
            condition = f'{prefix}.group_name IS NOT NULL OR {prefix}.origin IS NOT NULL'
        yield (f'CREATE TRIGGER rec_capture_playlist_scope_preferences_{event.lower()} AFTER {event} '
               f'ON playlist_scopes WHEN {condition} '
               "BEGIN INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
               'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')
        yield (f'CREATE TRIGGER rec_evidence_playlist_scope_preferences_{event.lower()} AFTER {event} '
               f'ON playlist_scopes WHEN {condition} '
               'BEGIN UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')


def _preflight(conn):
    for table, column in (('overlap_ignored', 'created_at'), ('album_library_memberships', 'saved_at'),
                          ('album_library_snapshots', 'observed_at'), ('album_library_legacy_cohort', 'observed_at')):
        for row in conn.execute(f'SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL'):
            value = row[1]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise MigrationError(f'Invalid {table} {column} at row {row[0]}')
    duplicate = conn.execute('SELECT provider_id,kind FROM playlist_scopes WHERE identity_id IS NULL '
        'GROUP BY provider_id,kind HAVING count(*)>1 LIMIT 1').fetchone()
    if duplicate:
        raise MigrationError(f'Duplicate unowned playlist scope: {tuple(duplicate)!r}')
    # No external owner references the old saved-membership surrogate IDs. Do
    # not silently discard them if an installed extension introduced such a FK.
    for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        quoted = '"' + name.replace('"', '""') + '"'
        if any(row[2] == 'album_library_memberships' for row in conn.execute(f'PRAGMA foreign_key_list({quoted})')):
            raise MigrationError(f'Cannot consolidate externally referenced album membership IDs: {name}')


def apply(conn, context):
    _preflight(conn)
    for column in SCOPE_COLUMNS:
        conn.execute('ALTER TABLE playlist_scopes ADD COLUMN ' + column)
    conn.execute('UPDATE playlist_scopes SET '
        'group_name=(SELECT name FROM playlist_group WHERE ytm=ref),'
        'hidden=EXISTS(SELECT 1 FROM hidden_playlists WHERE ytm=ref),'
        'overlap_ignored=EXISTS(SELECT 1 FROM overlap_ignored WHERE ytm=ref),'
        'overlap_ignored_at=(SELECT created_at FROM overlap_ignored WHERE ytm=ref),'
        'origin=(SELECT origin FROM playlist_provenance WHERE ytm=ref),'
        'adopted=(SELECT adopted FROM playlist_provenance WHERE ytm=ref),'
        'provenance_description=(SELECT description FROM playlist_provenance WHERE ytm=ref)')
    # Separate data installation from triggers so an upgrade does not replay
    # user activity or alter preparation/revision counters.
    definitions = tuple(statements())
    for sql in definitions:
        if sql.startswith('CREATE TABLE') or sql.startswith('CREATE INDEX ix_account_album'):
            conn.execute(sql)
    conn.execute('INSERT INTO account_album_state '
        'SELECT i.id,COALESCE(r.revision,0),s.observed_at,c.identity_id IS NOT NULL,c.observed_at '
        'FROM identities i LEFT JOIN album_library_revisions r ON r.identity_id=i.id '
        'LEFT JOIN album_library_snapshots s ON s.identity_id=i.id '
        'LEFT JOIN album_library_legacy_cohort c ON c.identity_id=i.id')
    conn.execute('INSERT INTO account_albums SELECT identity_id,album_id,source,saved_at '
                 'FROM album_library_memberships WHERE identity_id IS NOT NULL')
    conn.execute('INSERT INTO unattributed_album_memberships SELECT album_id,source,saved_at '
                 'FROM album_library_memberships WHERE identity_id IS NULL')
    conn.execute('DROP VIEW quarantined_playlists')
    # These two public read projections retain their established domain meaning.
    # They are read-only; old table names do not become writable aliases.
    conn.execute('DROP VIEW saved_albums')
    for table in ('playlist_group', 'hidden_playlists', 'overlap_ignored', 'playlist_provenance',
                  'album_library_revisions', 'album_library_snapshots', 'album_library_legacy_cohort',
                  'album_library_memberships'):
        conn.execute('DROP TABLE ' + table)
    for sql in definitions:
        if not sql.startswith('CREATE TABLE') and not sql.startswith('CREATE INDEX ix_account_album'):
            conn.execute(sql)
    conn.execute("CREATE VIEW saved_albums AS SELECT p.provider_id browse_id,a.title,a.artist,a.year,a.type,a.thumbnail "
        "FROM albums a JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
        'WHERE EXISTS(SELECT 1 FROM saved_album_memberships m WHERE m.album_id=a.id)')


def validate(conn, *, foreign_keys=True):
    from . import current
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    actual = definitions.get('playlist_scopes')
    expected = current._schema_contract((SCOPE_TABLE,))['playlist_scopes'][1]
    if actual is None or current._table(conn, 'playlist_scopes', actual) != expected:
        raise MigrationError('Incompatible consolidated playlist scope constraints')
    for sql in statements():
        name = sql.split()[3 if sql.startswith('CREATE UNIQUE') else 2]
        if definitions.get(name) != sql:
            raise MigrationError(f'Incompatible consolidated scope/account state: {name}')
    expected = {column.split()[0] for column in SCOPE_COLUMNS}
    if not expected <= {row[1] for row in conn.execute('PRAGMA table_info(playlist_scopes)').fetchall()}:
        raise MigrationError('Missing consolidated playlist scope attributes')
    if foreign_keys and conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid consolidated scope/account reference')
