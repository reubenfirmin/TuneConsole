-- Independent, populated pre-enrichment layout; intentionally does not import application DDL.
CREATE TABLE tracks (id INTEGER PRIMARY KEY,video_id TEXT,title TEXT,artist TEXT,
 album TEXT,duration_s INTEGER,identity_key TEXT NOT NULL);
INSERT INTO tracks VALUES (17,'original-video','Original title','Artist','Album',240,'original title|artist');
CREATE TABLE identities (id INTEGER PRIMARY KEY,label TEXT NOT NULL,credential_ref TEXT NOT NULL,
 brand_account_id TEXT,is_master INTEGER NOT NULL DEFAULT 0,last_auth_ok REAL,UNIQUE(label));
INSERT INTO identities VALUES (3,'main','credential',NULL,1,100);
CREATE TABLE playlists (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),
 ytm_playlist_id TEXT NOT NULL,title TEXT,track_count INTEGER,content_hash TEXT,
 first_seen REAL,last_seen REAL,last_changed REAL,UNIQUE(identity_id,ytm_playlist_id));
INSERT INTO playlists VALUES (8,3,'PLAYLIST','Collection',1,'original-hash',100,200,100);
CREATE TABLE playlist_tracks (playlist_id INTEGER NOT NULL REFERENCES playlists(id),
 track_id INTEGER NOT NULL REFERENCES tracks(id),position INTEGER NOT NULL);
INSERT INTO playlist_tracks VALUES (8,17,0);
CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT);
INSERT INTO settings VALUES ('user-setting','keep me');
