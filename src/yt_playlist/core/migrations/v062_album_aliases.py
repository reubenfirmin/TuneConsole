"""Several verified provider representations may identify one album release."""
from . import v023_albums


REPLACED = {'album_provider_ids', 'saved_albums', 'discovered_albums',
            'album_track_links', 'album_tracklists'}


def statements():
    yield ('CREATE TABLE album_provider_ids (provider TEXT NOT NULL,provider_id TEXT NOT NULL,'
           'album_id INTEGER NOT NULL REFERENCES albums(id),'
           'is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0,1)),'
           'equivalence_digest TEXT,verified_at REAL,PRIMARY KEY(provider,provider_id),'
           'CHECK(is_primary=1 OR (equivalence_digest IS NOT NULL AND verified_at IS NOT NULL)))')
    yield ('CREATE UNIQUE INDEX ix_album_primary_provider ON album_provider_ids(album_id,provider) '
           'WHERE is_primary=1')
    yield 'CREATE INDEX ix_album_provider_release ON album_provider_ids(album_id,provider,provider_id)'
    yield ('CREATE TABLE album_provider_fingerprints (provider TEXT NOT NULL,provider_id TEXT NOT NULL,'
           'digest TEXT NOT NULL,observed_at REAL NOT NULL,PRIMARY KEY(provider,provider_id),'
           'FOREIGN KEY(provider,provider_id) REFERENCES album_provider_ids(provider,provider_id))')
    yield 'CREATE INDEX ix_album_fingerprint ON album_provider_fingerprints(provider,digest)'
    yield ('CREATE TABLE album_alias_reconciliations (retired_id INTEGER PRIMARY KEY REFERENCES albums(id),'
           'album_id INTEGER NOT NULL REFERENCES albums(id),comparison_digest TEXT NOT NULL,'
           'verified_at REAL NOT NULL,CHECK(retired_id<>album_id))')
    yield ('CREATE TABLE album_alias_checks (first_id TEXT NOT NULL,second_id TEXT NOT NULL,'
           'checked_at REAL NOT NULL,PRIMARY KEY(first_id,second_id),CHECK(first_id<second_id))')
    yield 'CREATE INDEX ix_album_alias_check_time ON album_alias_checks(checked_at)'
    yield ('CREATE VIEW album_primary_provider_ids AS SELECT provider,provider_id,album_id '
           'FROM album_provider_ids WHERE is_primary=1')
    for sql in v023_albums.PROJECTIONS:
        if sql.startswith(('CREATE VIEW saved_albums ', 'CREATE VIEW discovered_albums ')):
            yield sql.replace('JOIN album_provider_ids p ', 'JOIN album_primary_provider_ids p ')
        elif sql.startswith('CREATE VIEW album_track_links '):
            yield sql.replace('GROUP BY t.album_id,t.entry_id', 'GROUP BY p.provider_id,t.entry_id')
        else:
            yield sql


def apply(conn, context):
    # The four dependent views and their INSTEAD OF triggers must be absent while
    # replacing their source table. The runner owns this atomic transaction.
    for name in sorted(REPLACED - {'album_provider_ids'}):
        conn.execute(f'DROP VIEW {name}')
    conn.execute('CREATE TEMP TABLE album_ids_before AS SELECT * FROM album_provider_ids')
    conn.execute('DROP TABLE album_provider_ids')
    for sql in statements():
        conn.execute(sql)
    conn.execute('INSERT INTO album_provider_ids(provider,provider_id,album_id) '
                 'SELECT provider,provider_id,album_id FROM album_ids_before')
    conn.execute('DROP TABLE album_ids_before')


def validate(conn):
    from .current import _table, _tokens
    from .runner import MigrationError
    import sqlite3
    reference = sqlite3.connect(':memory:')
    try:
        reference.execute('CREATE TABLE albums(id INTEGER PRIMARY KEY)')
        for sql in statements():
            reference.execute(sql)
        for name, kind, sql in reference.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name<>'albums' AND sql IS NOT NULL"):
            actual = conn.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if actual is None or actual[0] != kind:
                raise MigrationError(f'Missing album alias structure: {name}')
            if kind == 'table':
                matches = _table(conn, name, actual[1]) == _table(reference, name, sql)
            else:
                matches = _tokens(actual[1]) == _tokens(sql)
            if not matches:
                raise MigrationError(f'Incompatible album alias structure: {name}')
    finally:
        reference.close()
    if conn.execute('SELECT 1 FROM album_provider_ids GROUP BY album_id,provider '
                    'HAVING SUM(is_primary)<>1 LIMIT 1').fetchone():
        raise MigrationError('Album representations require exactly one preferred provider ID')
