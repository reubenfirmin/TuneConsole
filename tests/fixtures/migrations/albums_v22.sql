-- Installed version 22 data, independent of current album writers.
INSERT INTO recordings(id,identity_key,title,artist) VALUES (1,'song|artist','Song','Artist');
INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist,album,album_browse_id) VALUES ('v',1,'Song','Artist','Song','Artist','Album','release');
INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v');
INSERT INTO library_memberships VALUES (1,'fixture',NULL);
INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',1);
INSERT INTO artist_aliases VALUES (1,'Artist','legacy_credit');
INSERT INTO track_artists VALUES (1,0,1,'Artist','legacy_literal');
INSERT INTO saved_albums VALUES ('release','Album','Artist','1990','Album','cover');
INSERT INTO discovered_albums(browse_id,artist,title,year,thumbnail,found_at,first_shown,last_shown,offered_count,artist_id) VALUES ('release','Artist','Album','1990','cover',5,10,10,1,1);
INSERT INTO album_track_links(browse_id,recording_id,entry_id,video_id,source) VALUES ('release',1,1,'v','legacy');
INSERT INTO album_tracklists VALUES ('release','Album','Artist','cover','["song|artist"]');
