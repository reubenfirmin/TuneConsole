"""Keep retired artist names without treating an unresolved display name as an identity key."""
from .runner import MigrationError
from . import v021_artists


INDEX = 'CREATE INDEX ix_artist_provisional_name ON artists(name) WHERE provisional=1'
# Version 21 is frozen. This version owns the replacement contract rather than
# changing the historical validator that installed databases must pass first.
SQL = tuple(INDEX if sql.startswith('CREATE UNIQUE INDEX ix_artist_provisional_name ')
            else sql for sql in v021_artists.SQL)


def apply(conn,context):
    conn.execute('DROP INDEX ix_artist_provisional_name')
    conn.execute(INDEX)
    validate(conn)


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0]!=sql:
            raise MigrationError(f'Incompatible artist catalog: {name}')
    for table in v021_artists.STATE:
        columns = {r[1]:r for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'artist_id' not in columns or columns['artist_id'][2]!='INTEGER':
            raise MigrationError(f'Missing artist reference: {table}')
        if conn.execute(f'SELECT 1 FROM {table} WHERE artist_id IS NULL LIMIT 1').fetchone():
            raise MigrationError(f'Unresolved artist state: {table}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid artist catalog reference')
