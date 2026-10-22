"""Retain artist handles when contextual provider evidence reconciles a provisional ID."""
from .runner import MigrationError


SQL = (
    'CREATE TABLE artist_redirects (retired_id INTEGER PRIMARY KEY REFERENCES artists(id),artist_id INTEGER NOT NULL REFERENCES artists(id),source TEXT NOT NULL,created_at REAL NOT NULL,CHECK(retired_id<>artist_id))',
    'CREATE INDEX ix_artist_redirect_target ON artist_redirects(artist_id)',
    "CREATE TRIGGER artist_redirect_insert BEFORE INSERT ON artist_redirects WHEN EXISTS(SELECT 1 FROM artist_redirects WHERE retired_id=NEW.artist_id) OR NOT EXISTS(SELECT 1 FROM artists WHERE id=NEW.retired_id AND provisional=1) OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=NEW.retired_id) OR NOT EXISTS(SELECT 1 FROM artists a JOIN artist_provider_ids p ON p.artist_id=a.id WHERE a.id=NEW.artist_id AND a.provisional=0) BEGIN SELECT RAISE(ABORT,'Artist redirect requires a provisional source and canonical target'); END",
    "CREATE TRIGGER artist_redirect_update BEFORE UPDATE ON artist_redirects WHEN NEW.retired_id<>OLD.retired_id OR EXISTS(SELECT 1 FROM artist_redirects WHERE retired_id=NEW.artist_id) OR NOT EXISTS(SELECT 1 FROM artists WHERE id=NEW.retired_id AND provisional=1) OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=NEW.retired_id) OR NOT EXISTS(SELECT 1 FROM artists a JOIN artist_provider_ids p ON p.artist_id=a.id WHERE a.id=NEW.artist_id AND a.provisional=0) BEGIN SELECT RAISE(ABORT,'Artist redirect requires a provisional source and canonical target'); END",
)


def apply(conn,context):
    # Existing names provide no new identity evidence. Reconciliation happens only
    # when a later structured observation supplies context, never during upgrade.
    for sql in SQL:
        conn.execute(sql)


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0]!=sql:
            raise MigrationError(f'Incompatible artist redirect structure: {name}')
    if conn.execute('SELECT 1 FROM artist_redirects r JOIN artist_redirects target '
                    'ON target.retired_id=r.artist_id LIMIT 1').fetchone():
        raise MigrationError('Artist redirects must point directly to canonical identities')
    if conn.execute('SELECT 1 FROM artist_redirects r JOIN artists old ON old.id=r.retired_id '
                    'JOIN artists target ON target.id=r.artist_id WHERE old.provisional<>1 '
                    'OR target.provisional<>0 '
                    'OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.retired_id) '
                    'OR NOT EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.artist_id) '
                    'LIMIT 1').fetchone():
        raise MigrationError('Artist redirect requires a provisional source and canonical target')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid artist redirect reference')
