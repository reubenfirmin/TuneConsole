"""Canonical releases, relational track occurrences, and independent album membership."""
import json

from . import v008_recordings, v018_discovery_sources
from .runner import MigrationError


REPLACED = {'saved_albums','album_tracklists','discovered_albums'}
TABLES = (
    'CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,artist TEXT,year TEXT,type TEXT,thumbnail TEXT,genre TEXT,metadata_rank INTEGER NOT NULL,legacy_key TEXT UNIQUE)',
    'CREATE TABLE album_provider_ids (provider TEXT NOT NULL,provider_id TEXT NOT NULL,album_id INTEGER NOT NULL REFERENCES albums(id),PRIMARY KEY(provider,provider_id),UNIQUE(album_id,provider))',
    'CREATE TABLE album_artists (album_id INTEGER NOT NULL REFERENCES albums(id),position INTEGER NOT NULL CHECK(position>=0),artist_id INTEGER NOT NULL REFERENCES artists(id),credit TEXT NOT NULL,PRIMARY KEY(album_id,position))',
    'CREATE INDEX ix_album_artist ON album_artists(artist_id,album_id)',
    'CREATE TABLE album_library_memberships (id INTEGER PRIMARY KEY,album_id INTEGER NOT NULL REFERENCES albums(id),identity_id INTEGER REFERENCES identities(id),source TEXT NOT NULL,saved_at REAL,UNIQUE(album_id,identity_id))',
    'CREATE UNIQUE INDEX ix_album_library_unknown ON album_library_memberships(album_id) WHERE identity_id IS NULL',
    'CREATE TABLE album_discovery_memberships (album_id INTEGER PRIMARY KEY REFERENCES albums(id),found_at REAL,last_shown REAL,first_shown REAL,offered_count INTEGER NOT NULL DEFAULT 0,legacy_offered_count INTEGER NOT NULL DEFAULT 0,legacy_first_shown REAL,legacy_last_shown REAL)',
    'CREATE TABLE album_tracks (id INTEGER PRIMARY KEY,album_id INTEGER NOT NULL REFERENCES albums(id),entry_id INTEGER REFERENCES catalog_entries(id),recording_id INTEGER REFERENCES recordings(id),legacy_track_key TEXT,position INTEGER CHECK(position>=0),disc_number INTEGER CHECK(disc_number>0),track_number INTEGER CHECK(track_number>0),is_full INTEGER NOT NULL CHECK(is_full IN (0,1)),source TEXT NOT NULL,CHECK(entry_id IS NOT NULL OR recording_id IS NOT NULL OR legacy_track_key IS NOT NULL),CHECK(entry_id IS NULL OR recording_id IS NULL),UNIQUE(album_id,position))',
    'CREATE INDEX ix_album_track_entry ON album_tracks(entry_id,album_id)',
    'CREATE INDEX ix_album_track_recording ON album_tracks(recording_id,album_id)',
    'CREATE UNIQUE INDEX ix_album_track_incidental ON album_tracks(album_id,entry_id) WHERE is_full=0',
    'CREATE TABLE album_fetch_state (album_id INTEGER PRIMARY KEY REFERENCES albums(id),fetched_at REAL,complete INTEGER NOT NULL CHECK(complete IN (0,1)),expected_track_count INTEGER NOT NULL CHECK(expected_track_count>=0))',
)
PROJECTIONS = (
    "CREATE VIEW saved_albums AS SELECT p.provider_id browse_id,a.title,a.artist,a.year,a.type,a.thumbnail FROM albums a JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' WHERE EXISTS(SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id)",
    "CREATE VIEW discovered_albums AS SELECT p.provider_id browse_id,a.artist,a.title,a.year,a.thumbnail,m.found_at,m.last_shown,a.genre,m.first_shown,m.offered_count,m.legacy_offered_count,m.legacy_first_shown,m.legacy_last_shown,(SELECT artist_id FROM album_artists aa WHERE aa.album_id=a.id ORDER BY position LIMIT 1) artist_id FROM album_discovery_memberships m JOIN albums a ON a.id=m.album_id JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube'",
    "CREATE TRIGGER discovered_albums_update INSTEAD OF UPDATE ON discovered_albums BEGIN UPDATE album_discovery_memberships SET found_at=NEW.found_at,last_shown=NEW.last_shown,first_shown=NEW.first_shown,offered_count=NEW.offered_count,legacy_offered_count=NEW.legacy_offered_count,legacy_first_shown=NEW.legacy_first_shown,legacy_last_shown=NEW.legacy_last_shown WHERE album_id=(SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=OLD.browse_id); END",
    "CREATE TRIGGER discovered_albums_delete INSTEAD OF DELETE ON discovered_albums BEGIN DELETE FROM album_discovery_memberships WHERE album_id=(SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=OLD.browse_id); END",
    "CREATE VIEW album_track_links AS SELECT MIN(t.id) id,p.provider_id browse_id,e.recording_id,t.entry_id,e.video_id,MIN(t.source) source FROM album_tracks t JOIN album_provider_ids p ON p.album_id=t.album_id AND p.provider='youtube' JOIN catalog_entries e ON e.id=t.entry_id GROUP BY t.album_id,t.entry_id",
    "CREATE TRIGGER album_track_links_delete INSTEAD OF DELETE ON album_track_links BEGIN DELETE FROM album_tracks WHERE album_id=(SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=OLD.browse_id) AND entry_id=OLD.entry_id; END",
    "CREATE VIEW album_tracklists AS SELECT p.provider_id browse_id,a.title,a.artist,a.thumbnail,(SELECT json_group_array(k) FROM (SELECT COALESCE(r.identity_key,t.legacy_track_key) k FROM album_tracks t LEFT JOIN catalog_entries e ON e.id=t.entry_id LEFT JOIN recordings r ON r.id=COALESCE(e.recording_id,t.recording_id) WHERE t.album_id=a.id AND t.is_full=1 ORDER BY t.position IS NULL,t.position,t.id)) track_keys FROM album_fetch_state f JOIN albums a ON a.id=f.album_id JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' WHERE f.complete=1",
)


def apply(conn, context):
    for sql in TABLES:
        conn.execute(sql)

    def artist_id(name, explicit=None):
        if explicit is not None:
            return explicit
        matches = {r[0] for r in conn.execute('SELECT artist_id FROM artist_aliases WHERE alias=?', (name,))}
        if len(matches)==1:
            return next(iter(matches))
        row = conn.execute('SELECT id FROM artists WHERE name=? AND provisional=1', (name,)).fetchone()
        aid = row[0] if row else conn.execute('INSERT INTO artists(name,provisional) VALUES (?,1)', (name,)).lastrowid
        conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'legacy_album_credit')", (aid,name))
        return aid

    def album(browse, row, rank, explicit_artist=None):
        title, credit = row.get('title'), row.get('artist')
        legacy = None if browse else json.dumps([title,credit],ensure_ascii=False)
        found = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone() if browse else conn.execute('SELECT id FROM albums WHERE legacy_key=?', (legacy,)).fetchone()
        fields = ('title','artist','year','type','thumbnail','genre')
        values = [None if row.get(k)=='' else row.get(k) for k in fields]
        if found:
            aid = found[0]
            prior = conn.execute('SELECT metadata_rank FROM albums WHERE id=?', (aid,)).fetchone()[0]
            assignments = ','.join(f'{f}=COALESCE(?,{f})' if rank>=prior else f'{f}=COALESCE({f},?)' for f in fields)
            conn.execute(f'UPDATE albums SET {assignments},metadata_rank=MAX(metadata_rank,?) WHERE id=?', (*values,rank,aid))
        else:
            aid = conn.execute('INSERT INTO albums(title,artist,year,type,thumbnail,genre,metadata_rank,legacy_key) VALUES (?,?,?,?,?,?,?,?)', (*values,rank,legacy)).lastrowid
            if browse:
                conn.execute("INSERT INTO album_provider_ids VALUES ('youtube',?,?)", (browse,aid))
        if credit and (explicit_artist is not None or not conn.execute('SELECT 1 FROM album_artists WHERE album_id=?', (aid,)).fetchone()):
            conn.execute('INSERT INTO album_artists VALUES (?,0,?,?) ON CONFLICT(album_id,position) DO UPDATE SET artist_id=excluded.artist_id,credit=excluded.credit', (aid,artist_id(credit,explicit_artist),credit))
        return aid

    # Retain the original metadata and cache payloads before replacing their storage.
    for table in ('saved_albums','discovered_albums','album_tracklists','album_track_links'):
        for row in conn.execute(f'SELECT * FROM {table}').fetchall():
            conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                         ('album_legacy:'+table,str(row['id'] if table=='album_track_links' else row['browse_id']),
                          json.dumps(dict(row)),'original album metadata or relationship'))
    for row in conn.execute("SELECT * FROM catalog_tracks WHERE NULLIF(album,'') IS NOT NULL OR NULLIF(album_browse_id,'') IS NOT NULL ORDER BY id").fetchall():
        album(row['album_browse_id'],{'title':row['album'],'artist':row['artist'],'thumbnail':row['thumbnail']},0)
    for row in conn.execute('SELECT * FROM album_track_links ORDER BY id').fetchall():
        aid = album(row['browse_id'],{},0)
        conn.execute('INSERT INTO album_tracks(album_id,entry_id,is_full,source) VALUES (?,?,0,?)', (aid,row['entry_id'],row['source']))
    # Title-only source credits remain separate provisional releases; no title-based
    # attachment to an identified release is justified by the legacy data.
    for row in conn.execute("SELECT * FROM catalog_tracks WHERE NULLIF(album,'') IS NOT NULL AND NULLIF(album_browse_id,'') IS NULL ORDER BY id").fetchall():
        aid = album(None,{'title':row['album'],'artist':row['artist'],'thumbnail':row['thumbnail']},0)
        conn.execute("INSERT OR IGNORE INTO album_tracks(album_id,entry_id,is_full,source) VALUES (?,?,0,'legacy_unidentified')", (aid,row['id']))
    for row in conn.execute('SELECT * FROM discovered_albums ORDER BY browse_id').fetchall():
        aid = album(row['browse_id'],dict(row),1,row['artist_id'])
        conn.execute('INSERT INTO album_discovery_memberships VALUES (?,?,?,?,?,?,?,?)',
                     (aid,*(row[k] for k in ('found_at','last_shown','first_shown','offered_count','legacy_offered_count','legacy_first_shown','legacy_last_shown'))))
    for row in conn.execute('SELECT * FROM saved_albums ORDER BY browse_id').fetchall():
        aid = album(row['browse_id'],dict(row),2)
        conn.execute("INSERT INTO album_library_memberships(album_id,source) VALUES (?,'legacy_combined')", (aid,))
    for row in conn.execute('SELECT * FROM album_tracklists ORDER BY browse_id').fetchall():
        aid = album(row['browse_id'],dict(row),3)
        try:
            keys = json.loads(row['track_keys'])
            valid = isinstance(keys,list) and all(isinstance(k,str) for k in keys)
        except (ValueError,TypeError):
            keys, valid = [], False
        conn.execute('INSERT INTO album_fetch_state VALUES (?,NULL,?,?)', (aid,int(valid),len(keys) if valid else 0))
        if not valid:
            continue  # The original malformed payload remains archived; it proves no completeness.
        for key in dict.fromkeys(keys):
            candidates = conn.execute('SELECT t.id FROM album_tracks t JOIN catalog_entries e ON e.id=t.entry_id JOIN recordings r ON r.id=e.recording_id WHERE t.album_id=? AND r.identity_key=?', (aid,key)).fetchall()
            if candidates:
                conn.executemany('UPDATE album_tracks SET is_full=1 WHERE id=?', [(r[0],) for r in candidates])
                continue
            # A cache key may identify a recording while naming no exact release item.
            recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            conn.execute("INSERT INTO album_tracks(album_id,recording_id,legacy_track_key,is_full,source) VALUES (?,?,?,1,'legacy_full_fetch')", (aid,recording[0] if recording else None,key))
        conn.execute('UPDATE album_fetch_state SET expected_track_count=(SELECT COUNT(*) FROM album_tracks WHERE album_id=? AND is_full=1) WHERE album_id=?', (aid,aid))
    for table in ('saved_albums','discovered_albums','album_tracklists','album_track_links'):
        conn.execute(f'DROP TABLE {table}')
    for sql in PROJECTIONS:
        conn.execute(sql)


def validate(conn):
    replaced = {'album_track_links','ix_album_recording'}
    schema = [(n,k,s) for n,k,s in v018_discovery_sources.expected_schema() if n not in replaced]
    for sql in (*TABLES,*PROJECTIONS):
        parts = sql.split()
        name = parts[3] if parts[1]=='UNIQUE' else parts[2]
        kind = 'index' if parts[1]=='UNIQUE' else parts[1].lower()
        schema.append((name,kind,sql))
    v008_recordings.validate(conn,schema=schema)
    # The album half of v22 now projects its artist from ordered release credits.
    # Keep validating its unchanged feedback reference on the physical table.
    columns = {r[1]:r[2] for r in conn.execute('PRAGMA table_info(rec_feedback)')}
    index = conn.execute("SELECT sql FROM sqlite_master WHERE name='ix_rec_feedback_artist'").fetchone()
    if columns.get('artist_id') != 'INTEGER' or not any(r[2]=='artists' and r[3]=='artist_id' and r[4]=='id' for r in conn.execute('PRAGMA foreign_key_list(rec_feedback)')) or not index or index[0] != 'CREATE INDEX ix_rec_feedback_artist ON rec_feedback(artist_id,kind)':
        raise MigrationError('Incompatible artist feedback reference')
