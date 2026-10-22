-- Installed version-24 layouts for the tables being rebuilt. These definitions
-- are deliberately independent of the draft migration's CREATE statements.
DROP TABLE enrichment_log;
DROP TABLE enrichment_conflict;
DROP TABLE rec_mode_impressions;
DROP TABLE rec_mode_picks;
DROP TABLE road_trip_drafts;
CREATE TABLE enrichment_log (
 id INTEGER PRIMARY KEY,track_id INTEGER NOT NULL,run_id TEXT NOT NULL,
 provider TEXT NOT NULL,field TEXT NOT NULL,value TEXT,created_at REAL,
 recording_id INTEGER REFERENCES recordings(id),input_video_id TEXT,
 field_scope TEXT NOT NULL DEFAULT 'legacy_item');
CREATE INDEX idx_enrichment_log_track ON enrichment_log(track_id);
CREATE TABLE enrichment_conflict (
 id INTEGER PRIMARY KEY,track_id INTEGER NOT NULL,field TEXT NOT NULL,
 candidates TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,resolved_value TEXT,
 updated_at REAL,recording_id INTEGER REFERENCES recordings(id),input_video_id TEXT,
 field_scope TEXT NOT NULL DEFAULT 'legacy_item',UNIQUE(track_id,field));
CREATE TABLE rec_mode_impressions (
 epoch INTEGER NOT NULL,lane TEXT NOT NULL,mode_id INTEGER NOT NULL,
 ranker TEXT,created_at REAL NOT NULL,PRIMARY KEY(epoch,lane));
CREATE TABLE rec_mode_picks (
 playlist_id INTEGER PRIMARY KEY,mode_id INTEGER NOT NULL,ranker TEXT,created_at REAL NOT NULL);
CREATE TABLE road_trip_drafts (recipe_id INTEGER PRIMARY KEY,state TEXT NOT NULL,updated_at REAL NOT NULL);
INSERT INTO identities(id,label,credential_ref,is_master,active) VALUES (1,'Main','c',1,1);
INSERT INTO playlist_scopes VALUES ('PL','PL',NULL,'shared'),('OTHER','OTHER',NULL,'shared');
INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,content_hash,first_seen,last_seen,last_changed,scope_ref)
 VALUES (1,1,'PL','Mix',3,'h',1,1,1,'PL'),(2,1,'OTHER','Other',1,'h',1,1,1,'OTHER');
INSERT INTO playlist_group VALUES ('PL','Keep');
INSERT INTO recordings(id,identity_key,title,artist) VALUES (1,'a|artist','A','Artist'),(2,'b|artist','B','Artist'),(3,'unknown|artist','Unknown','Artist');
INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES ('a',1,'A','Artist'),('b',2,'B','Artist');
INSERT INTO catalog_entries(id,recording_id,video_id,source_ref) VALUES (1,1,'a',NULL),(2,2,'b',NULL),(3,3,NULL,'unresolved:fixture');
INSERT INTO unresolved_items(entry_id,title,artist) VALUES (3,'Unknown','Artist');
INSERT INTO library_memberships VALUES (1,'fixture',1),(2,'fixture',1);
INSERT INTO legacy_track_refs VALUES (10,1,1,'a');
INSERT INTO playlist_items VALUES (1,'a',4),(1,'b',4),(2,'a',9);
INSERT INTO unresolved_playlist_items VALUES (1,3,-1);
INSERT INTO enrichment_log VALUES
 (1,1,'run','musicbrainz','genre','house',1,1,'a','recording'),
 (2,999,'run','lastfm','genre','orphan',2,NULL,NULL,'legacy_item'),
 (3,2,'run','lastfm','genre','rock',3,2,'missing-video','recording');
INSERT INTO enrichment_conflict VALUES
 (1,2,'genre','[{"provider":"lastfm","value":"rock"}]',0,NULL,1,2,'b','recording'),
 (2,999,'genre','[]',0,NULL,1,NULL,NULL,'legacy_item'),
 (3,10,'genre','[{"provider":"lastfm","value":"house"}]',1,'house',1,1,'a','legacy_item'),
 (4,1,'genre','[{"provider":"musicbrainz","value":"rock"}]',1,'rock',2,1,'a','recording');
INSERT INTO taste_modes(mode_id,label,families,centroid,size,rep_keys,active,first_seen,last_seen)
 VALUES (7,'House','[]',X'0000803f',3,'[]',1,1,1);
INSERT INTO rec_mode_impressions VALUES (1,'known',7,'cosine',10),(1,'missing',999,'ppr',10);
INSERT INTO rec_mode_picks VALUES (1,7,'cosine',20),(999,999,'ppr',20);
INSERT INTO road_trip_recipes(id,name,own_pct,artists,genres,blacklist_genres,target_minutes,created_at,updated_at)
 VALUES (1,'Trip',50,'[]','[]','[]',30,1,1);
INSERT INTO road_trip_drafts VALUES (1,'{"picked":[]}',1),(999,'{"legacy":"orphan"}',1);
