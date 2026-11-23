"""Put preferences and retired identity links on their genre rows."""
from .. import catalog_metadata, group_preview_work
from ..schema_contract import MigrationError, tokens


NAME = 'fold_genre_storage'
TABLES = ('genre_preferences', 'genre_redirects', 'genre_external_id', 'genre_merge_preferences')
COLUMNS = (
    "enabled INTEGER NOT NULL DEFAULT 0 CHECK(typeof(enabled)='integer' AND enabled IN (0,1))",
    "preference_origin TEXT CHECK(preference_origin IN ('default','custom','user','legacy') "
    "AND (enabled=0 OR preference_origin IS NOT NULL))",
    'redirect_key TEXT REFERENCES genre_catalog(key) CHECK(redirect_key<>key)',
)
INDEXES = (
    'CREATE INDEX ix_genre_enabled ON genre_catalog(key) WHERE enabled=1',
    'CREATE INDEX ix_genre_redirect_target ON genre_catalog(redirect_key) WHERE redirect_key IS NOT NULL',
)
GUARDS = (
    '''CREATE TRIGGER genre_catalog_redirect_insert BEFORE INSERT ON genre_catalog
WHEN NEW.redirect_key IS NOT NULL AND EXISTS(
    SELECT 1 FROM genre_catalog
    WHERE (key=NEW.redirect_key AND redirect_key IS NOT NULL) OR redirect_key=NEW.key)
BEGIN SELECT RAISE(ABORT,'Genre redirects must point directly to a current identity'); END''',
    '''CREATE TRIGGER genre_catalog_redirect_update BEFORE UPDATE OF key,redirect_key ON genre_catalog
WHEN (OLD.redirect_key IS NOT NULL AND (NEW.redirect_key IS NULL OR OLD.key<>NEW.key)) OR (
    NEW.redirect_key IS NOT NULL AND EXISTS(SELECT 1 FROM genre_catalog
        WHERE (key=NEW.redirect_key AND redirect_key IS NOT NULL) OR redirect_key=OLD.key))
BEGIN SELECT RAISE(ABORT,'Genre redirects must point directly to a current identity'); END''',
    '''CREATE TRIGGER genre_catalog_redirect_retained BEFORE DELETE ON genre_catalog
WHEN OLD.redirect_key IS NOT NULL
BEGIN SELECT RAISE(ABORT,'Genre merge lineage is retained'); END''',
)


def predecessor_contract(conn, baseline):
    names = ('genre_catalog', *TABLES)
    prior = {name: value for name, value in baseline.items() if name in names or name.startswith(
        ('genre_redirect_', 'ix_genre_preference_'))}
    prior.update((name, ('trigger', tokens(sql))) for name, sql in
                 group_preview_work.guards(conn, sources={'genre_catalog', 'genre_redirects'}))
    return prior


def install(conn, _context):
    for column in COLUMNS:
        conn.execute('ALTER TABLE genre_catalog ADD COLUMN ' + column)
    conn.execute('UPDATE genre_catalog SET (enabled,preference_origin)='
                 '(SELECT enabled,origin FROM genre_preferences WHERE genre_key=genre_catalog.key) '
                 'WHERE key IN (SELECT genre_key FROM genre_preferences)')
    conn.execute('UPDATE genre_catalog SET redirect_key='
                 '(SELECT genre_key FROM genre_redirects WHERE retired_key=genre_catalog.key) '
                 'WHERE key IN (SELECT retired_key FROM genre_redirects)')
    conn.execute('DROP VIEW catalog_tracks')
    for table in TABLES:
        conn.execute('DROP TABLE ' + table)
    conn.execute(catalog_metadata.track_view(conn, canonical=True, inline_genre_redirects=True))
    for sql in (*INDEXES, *GUARDS):
        conn.execute(sql)
    group_preview_work.install_guards(conn, sources={'genre_catalog'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate(conn):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN (?,?,?,?)', TABLES).fetchone():
        raise MigrationError('Retired genre storage remains')
