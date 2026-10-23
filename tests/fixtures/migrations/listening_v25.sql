-- Independent installed layouts for the evidence read by migration 26. No new
-- listening tables, timezone claims, or deduplication assumptions appear here.
CREATE TABLE identities (id INTEGER PRIMARY KEY);
CREATE TABLE recordings (id INTEGER PRIMARY KEY,identity_key TEXT NOT NULL);
CREATE TABLE history_snapshots (
 id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),taken_at REAL NOT NULL);
CREATE TABLE history_items (
 snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id),identity_key TEXT NOT NULL,
 recording_id INTEGER REFERENCES recordings(id),raw_identity_key TEXT,
 source TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE play_events (
 id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),identity_key TEXT NOT NULL,
 video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT,
 is_generated INTEGER CHECK(is_generated IN (0,1)),source TEXT NOT NULL DEFAULT '',
 recording_id INTEGER REFERENCES recordings(id),raw_identity_key TEXT);
CREATE TABLE player_events (
 id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),kind TEXT NOT NULL,
 video_id TEXT,position REAL,duration REAL,playlist_ytm_id TEXT,payload TEXT,at REAL NOT NULL);
CREATE TABLE trend_first_play (
 kind TEXT NOT NULL,id_key TEXT NOT NULL,first_day INTEGER NOT NULL,first_ts REAL NOT NULL,
 source TEXT NOT NULL,PRIMARY KEY(kind,id_key));
CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT);
INSERT INTO identities VALUES (1),(2);
INSERT INTO recordings VALUES (1,'song|artist');
INSERT INTO history_snapshots VALUES (1,1,1792540800),(2,1,1792540801),(3,2,1792540800);
INSERT INTO history_items VALUES
 (1,'song|artist',1,'original','unknown'),(2,'song|artist',1,'original','unknown'),
 (3,'song|artist',1,'original','ytm_history');
INSERT INTO play_events VALUES
 (1,1,'song|artist','v',1792555200,NULL,NULL,NULL,'takeout',1,'original'),
 (2,2,'song|artist','v',1792555200,'generated',NULL,1,'live',1,'original');
INSERT INTO player_events VALUES (1,1,'track_exit','v',12.5,200,'p','{"unchanged":true}',1792555200);
INSERT INTO trend_first_play VALUES ('track','song|artist',0,0,'history');
INSERT INTO settings VALUES ('trend_first_play_watermark','3'),('trend_first_play_source','history');
