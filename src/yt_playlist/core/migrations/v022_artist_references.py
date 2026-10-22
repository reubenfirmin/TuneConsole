"""Complete artist references on databases that have already applied version 21."""
import json

from .runner import MigrationError


INDEXES = (
    'CREATE INDEX ix_discovered_album_artist ON discovered_albums(artist_id)',
    'CREATE INDEX ix_rec_feedback_artist ON rec_feedback(artist_id,kind)',
)


def apply(conn, context):
    # Development databases may have either addition from a later draft of 21.
    # Keep existing canonical references and fill only the missing ones.
    for table in ('discovered_albums','rec_feedback'):
        if not any(r[1]=='artist_id' for r in conn.execute(f'PRAGMA table_info({table})')):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN artist_id INTEGER REFERENCES artists(id)')
    for sql in INDEXES:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (sql.split()[2],)).fetchone():
            conn.execute(sql)

    def resolve(name, *, numeric=False):
        matches = {r[0] for r in conn.execute('SELECT artist_id FROM artist_aliases WHERE alias=?', (name,))}
        if len(matches)==1:
            return next(iter(matches))
        if name.startswith('@artist:') or (numeric and name.isdecimal()):
            try:
                aid = int(name.removeprefix('@artist:'))
            except ValueError:
                pass
            else:
                if conn.execute('SELECT 1 FROM artists WHERE id=?', (aid,)).fetchone():
                    return aid
        row = conn.execute('SELECT id FROM artists WHERE name=? AND provisional=1', (name,)).fetchone()
        aid = row[0] if row else conn.execute('INSERT INTO artists(name,provisional) VALUES (?,1)', (name,)).lastrowid
        conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'legacy_credit')", (aid,name))
        return aid

    # Earlier v21 databases did not retain original display credits as aliases.
    # Attach one only where the track has a single canonical credit.
    for row in conn.execute('SELECT ta.artist_id,t.orig_artist FROM catalog_tracks t '
                            'JOIN track_artists ta ON ta.entry_id=t.id '
                            'WHERE NULLIF(t.orig_artist,\'\') IS NOT NULL '
                            'AND (SELECT COUNT(*) FROM track_artists a WHERE a.entry_id=t.id)=1').fetchall():
        conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'legacy_original_credit')", tuple(row))
    for row in conn.execute('SELECT browse_id,artist FROM discovered_albums WHERE artist_id IS NULL').fetchall():
        conn.execute('UPDATE discovered_albums SET artist_id=? WHERE browse_id=?',
                     (resolve(row['artist'] or ''),row['browse_id']))
    for row in conn.execute('SELECT rowid feedback_rowid,surface,item_key FROM rec_feedback WHERE artist_id IS NULL').fetchall():
        name = row['item_key'][7:] if row['item_key'].startswith('artist:') else (
            row['item_key'] if row['surface'] in ('artist','new_artists') else None)
        if name:
            conn.execute("UPDATE rec_feedback SET artist_id=?,target_type='artist' WHERE rowid=?",
                         (resolve(name),row['feedback_rowid']))

    impressions = conn.execute("SELECT * FROM discovery_visible_impressions WHERE kind='artist'").fetchall()
    conn.execute("DELETE FROM discovery_visible_impressions WHERE kind='artist'")
    for row in impressions:
        conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                     ('artist_visible_impression',json.dumps([row['view_id'],row['item_id']]),
                      json.dumps(dict(row)),'original artist visible impression'))
        aid = resolve(row['item_id'],numeric=True)
        conn.execute("INSERT INTO discovery_visible_impressions VALUES (?,'artist',?,?) "
                     "ON CONFLICT(view_id,kind,item_id) DO UPDATE SET shown_at=MIN(shown_at,excluded.shown_at)",
                     (row['view_id'],str(aid),row['shown_at']))


def validate(conn):
    for table in ('discovered_albums','rec_feedback'):
        columns = {r[1]:r for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'artist_id' not in columns or columns['artist_id'][2] != 'INTEGER':
            raise MigrationError(f'Missing artist reference: {table}')
        if not any(r[2]=='artists' and r[3]=='artist_id' and r[4]=='id'
                   for r in conn.execute(f'PRAGMA foreign_key_list({table})')):
            raise MigrationError(f'Missing artist foreign key: {table}')
    for sql in INDEXES:
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (sql.split()[2],)).fetchone()
        if row is None or row[0] != sql:
            raise MigrationError(f'Incompatible artist reference index: {sql.split()[2]}')
    if conn.execute('SELECT 1 FROM discovered_albums WHERE artist_id IS NULL LIMIT 1').fetchone():
        raise MigrationError('Unresolved artist state: discovered_albums')
