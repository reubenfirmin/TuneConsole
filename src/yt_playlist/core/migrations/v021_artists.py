"""Canonical artist IDs and ordered credits, retaining unsplit legacy display text."""
import json
import unicodedata
from collections import defaultdict

from .runner import MigrationError


SQL = (
    'CREATE TABLE artists (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,provisional INTEGER NOT NULL CHECK(provisional IN (0,1)))',
    'CREATE UNIQUE INDEX ix_artist_provisional_name ON artists(name) WHERE provisional=1',
    'CREATE TABLE artist_provider_ids (provider TEXT NOT NULL,provider_id TEXT NOT NULL,artist_id INTEGER NOT NULL REFERENCES artists(id),PRIMARY KEY(provider,provider_id))',
    'CREATE INDEX ix_artist_provider_subject ON artist_provider_ids(artist_id)',
    'CREATE TABLE artist_aliases (artist_id INTEGER NOT NULL REFERENCES artists(id),alias TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(artist_id,alias,source))',
    'CREATE INDEX ix_artist_alias ON artist_aliases(alias,artist_id)',
    'CREATE TABLE track_artists (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),position INTEGER NOT NULL CHECK(position>=0),artist_id INTEGER NOT NULL REFERENCES artists(id),credit TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(entry_id,position))',
    'CREATE INDEX ix_track_artist_subject ON track_artists(artist_id,entry_id)',
    'CREATE TABLE artist_credit_observations (id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),credit TEXT NOT NULL,credits_json TEXT NOT NULL,source TEXT NOT NULL,observed_at REAL)',
    'CREATE TABLE artist_similarity_targets (source_artist_id INTEGER NOT NULL REFERENCES artists(id),target_artist_id INTEGER NOT NULL REFERENCES artists(id),weight REAL NOT NULL,PRIMARY KEY(source_artist_id,target_artist_id))',
)
STATE = ('artist_scans','discovered_artists','rec_artist_similar')


def apply(conn, context):
    for sql in SQL:
        conn.execute(sql)

    def ensure(name, browse=None):
        row = (conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone()
               if browse else conn.execute('SELECT id FROM artists WHERE name=? AND provisional=1', (name,)).fetchone())
        if row:
            aid = row[0]
        else:
            aid = conn.execute('INSERT INTO artists(name,provisional) VALUES (?,?)',
                               (name,int(not browse))).lastrowid
            if browse:
                conn.execute("INSERT INTO artist_provider_ids VALUES ('youtube',?,?)", (browse,aid))
        conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'legacy_credit')", (aid,name))
        return aid

    for row in conn.execute('SELECT id,artist,orig_artist,artist_browse_id FROM catalog_tracks ORDER BY id').fetchall():
        if not row['artist']:
            continue
        aid = ensure(row['artist'],row['artist_browse_id'])
        if row['orig_artist']:
            conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'legacy_original_credit')",
                         (aid,row['orig_artist']))
        conn.execute("INSERT INTO track_artists VALUES (?,0,?,?,'legacy_literal')", (row['id'],aid,row['artist']))
        conn.execute("INSERT INTO artist_credit_observations(entry_id,credit,credits_json,source) VALUES (?,?,?,'legacy_literal')",
                     (row['id'],row['artist'],json.dumps([{'name':row['artist'],'id':row['artist_browse_id']}])))
    for row in conn.execute('SELECT DISTINCT artist FROM saved_albums WHERE NULLIF(artist,\'\') IS NOT NULL').fetchall():
        ensure(row[0])
    # Legacy state is name-scoped evidence. Keep its raw payload and attach only unambiguous names.
    for table in STATE:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN artist_id INTEGER REFERENCES artists(id)')
        groups = defaultdict(list)
        for row in conn.execute(f'SELECT * FROM {table}').fetchall():
            raw = dict(row)
            conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                         ('artist_state:'+table,row['artist'],json.dumps(raw),'original name-scoped artist state'))
            matches = {r[0] for r in conn.execute('SELECT artist_id FROM artist_aliases WHERE alias=?', (row['artist'],))}
            if not matches:
                # Old similar caches used normalized keys. A unique case-folded alias is evidence;
                # punctuation/accent stripping would incorrectly collapse unrelated legacy names.
                matches = {r['artist_id'] for r in conn.execute('SELECT artist_id,alias FROM artist_aliases')
                           if unicodedata.normalize('NFC',r['alias']).casefold() == unicodedata.normalize('NFC',row['artist']).casefold()}
            aid = next(iter(matches)) if len(matches)==1 else ensure(row['artist'])
            groups[aid].append(raw)
        for aid,rows in groups.items():
            clock = 'scanned_at' if table=='artist_scans' else 'fetched_at' if table=='rec_artist_similar' else 'found_at'
            rows.sort(key=lambda r:(r.get(clock) or 0,json.dumps(r,sort_keys=True)))
            for old in rows[:-1]:
                conn.execute(f'DELETE FROM {table} WHERE artist=?', (old['artist'],))
            conn.execute(f'UPDATE {table} SET artist_id=? WHERE artist=?', (aid,rows[-1]['artist']))
            if table == 'discovered_artists':
                def first(field):
                    return min((r[field] for r in rows if r.get(field) is not None),default=None)
                last_shown = max((r['last_shown'] for r in rows if r.get('last_shown') is not None),default=None)
                conn.execute('UPDATE discovered_artists SET found_at=?,first_shown=?,last_shown=?,'
                             'offered_count=?,legacy_offered_count=? WHERE artist_id=?',
                             (first('found_at'),first('first_shown'),last_shown,
                              sum(r['offered_count'] or 0 for r in rows),
                              sum(r['legacy_offered_count'] or 0 for r in rows),aid))
        conn.execute(f'CREATE UNIQUE INDEX ix_{table}_artist ON {table}(artist_id)')
    for row in conn.execute('SELECT artist_id,payload FROM rec_artist_similar').fetchall():
        try:
            pairs = json.loads(row['payload'])
        except (ValueError,TypeError):
            continue  # Raw cache evidence was archived above; malformed derived edges can expire.
        if not isinstance(pairs,list):
            continue
        for pair in pairs:
            if not isinstance(pair,(list,tuple)) or len(pair)!=2 or not isinstance(pair[0],str):
                continue
            name,weight = pair
            if not isinstance(weight,(int,float)):
                continue
            matches = {r[0] for r in conn.execute('SELECT artist_id FROM artist_aliases WHERE alias=?', (name,))}
            target = next(iter(matches)) if len(matches)==1 else ensure(name)
            conn.execute('INSERT OR REPLACE INTO artist_similarity_targets VALUES (?,?,?)',
                         (row['artist_id'],target,weight))
    # These vectors were built in the name vocabulary. Evidence and learned feedback are retained.
    for table in ('rec_artist_vectors','rec_artist_content_vectors'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("DELETE FROM settings WHERE key='rec_artist_content_model'")


def validate(conn):
    for sql in SQL:
        name = sql.split()[2] if sql.startswith('CREATE TABLE') or sql.startswith('CREATE INDEX') else sql.split()[3]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] != sql:
            raise MigrationError(f'Incompatible artist catalog: {name}')
    # This version shipped before feedback and album references. Validate only its
    # original contract so those databases can reach the forward migration.
    for table in STATE:
        columns = {r[1]:r for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'artist_id' not in columns or columns['artist_id'][2] != 'INTEGER':
            raise MigrationError(f'Missing artist reference: {table}')
        if conn.execute(f'SELECT 1 FROM {table} WHERE artist_id IS NULL LIMIT 1').fetchone():
            raise MigrationError(f'Unresolved artist state: {table}')
