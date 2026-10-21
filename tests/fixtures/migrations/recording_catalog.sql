-- Populated legacy catalog; no new catalog tables or migration implementation are imported.
INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','credential',1);
INSERT INTO tracks(id,video_id,title,artist,orig_title,orig_artist,identity_key,album_browse_id,genre,mb_recording_id,duration_s) VALUES
 (3,'upload-a','Song','Artist','Song','Artist','song|artist','release-a','House','same-mbid',200),
 (7,'upload-a','Corrected Song','Artist','Song','Artist','old spelling|artist','release-b','Techno','same-mbid',201),
 (8,'upload-live','Song (Live)','Artist','Song (Live)','Artist','song|artist','release-live','Rock','same-mbid',240),
 (9,'upload-remix','Song (Remix)','Artist','Song (Remix)','Artist','song|artist',NULL,NULL,'same-mbid',280),
 (10,NULL,'Unresolved','Artist','Unresolved','Artist','unresolved|artist','saved-release','Folk',NULL,180);
INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,content_hash) VALUES
 (1,1,'LM','Liked Music',3,'old-hash'),(2,1,'GENERATED','Generated mix',1,'old-generated-hash');
INSERT INTO playlist_tracks VALUES (1,3,0),(1,7,1),(1,8,2),(2,9,0);
INSERT INTO playlist_group VALUES ('GENERATED','Generated');
INSERT INTO discovered_albums(browse_id,artist,title,found_at) VALUES ('release-c','Artist','Album C',1);
INSERT INTO discovered_tracks(identity_key,video_id,title,artist,source_browse_id,found_at,first_shown,last_shown,offered_count,genre,thumbnail) VALUES
 ('discovery spelling|artist','upload-a','Song','Artist','release-c',20,30,40,4,'House','cover-a'),
 ('cold|other','cold-upload','Cold','Other','radio:seed',50,NULL,NULL,0,NULL,NULL);
INSERT INTO enrichment_log(track_id,run_id,provider,field,value,created_at) VALUES
 (7,'run','musicbrainz','genre','Techno',10);
INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,resolved_value,updated_at) VALUES
 (7,'genre','[{"provider":"musicbrainz","value":"Techno"},{"provider":"lastfm","value":"House"}]',1,'',11),
 (8,'bpm','[{"provider":"one","value":120},{"provider":"two","value":121}]',1,'121',12);
INSERT INTO play_events(identity_id,identity_key,video_id,played_at,playlist_ytm_id,is_generated,source) VALUES
 (1,'song|artist','upload-live',100,'LM',0,'live'),
 (1,'song|artist',NULL,200,NULL,NULL,'legacy'),
 (1,'old spelling|artist','unknown-upload',300,'GENERATED',1,'live');
INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (1,1,100);
INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'song|artist'),(1,'old spelling|artist');
INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES
 ('like','song|artist','like',10),('like_grad','song|artist','like_grad',10),
 ('for_you','old spelling|artist','less',11);
INSERT INTO rec_weights VALUES ('genre:house',1.3,10);
INSERT INTO album_tracklists(browse_id,title,artist,track_keys) VALUES ('release-b','Album B','Artist','["old spelling|artist"]');
INSERT INTO cluster_canvas(playlist_ytm,state) VALUES ('CLUSTER','{"nodes":[{"id":1,"key":"old spelling|artist","seeds":[{"keys":["old spelling|artist"]}]}],"trunk":[1],"label":"old spelling|artist"}');
INSERT INTO settings VALUES ('enrich_attempts:cold|other','{"deezer":{"at":60,"status":"found","fields":{"art":"full-cover","isrc":"ISRC-ONE","bpm":128}}}');
INSERT INTO settings VALUES ('enrich_attempts:song|artist','{"deezer":{"at":60,"status":"retry","fields":{}}}');
