-- Independent installed-v23 rows; no current playlist/account writers.
INSERT INTO identities(id,label,credential_ref,brand_account_id,is_master,active) VALUES (1,'First','browser.json','brand1',0,1),(2,'Main','browser.json',NULL,1,1);
INSERT INTO playlists(id,identity_id,ytm_playlist_id,title) VALUES (1,1,'LM','First likes'),(2,2,'LM','Main likes'),(3,1,'SE','Later'),(4,1,'shared','Shared'),(5,2,'shared','Shared');
INSERT INTO playlist_group VALUES ('LM','Old likes'),('shared','Keep');
INSERT INTO hidden_playlists VALUES ('LM'),('SE');
INSERT INTO suppressed_overlaps VALUES ('LM','shared',10);
INSERT INTO overlap_ignored VALUES ('shared',11);
INSERT INTO overlap_ignored VALUES ('LM',11);
INSERT INTO overlap_kept VALUES ('SE','shared',12);
INSERT INTO cleanup_ignored VALUES ('shared','tiny',13);
INSERT INTO cleanup_ignored VALUES ('LM','empty',13);
INSERT INTO ignored_merges VALUES ('SE|shared','["SE","shared"]',14);
INSERT INTO rec_recipes VALUES ('shared','{"model":"cluster"}',15);
INSERT INTO cluster_canvas VALUES ('shared','{"nodes":[]}',16);
