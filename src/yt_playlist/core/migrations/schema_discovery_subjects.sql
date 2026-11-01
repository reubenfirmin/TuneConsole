CREATE TABLE artist_scans (
 artist_id INTEGER PRIMARY KEY NOT NULL REFERENCES artists(id), scanned_at REAL
);
CREATE TABLE discovered_artists (
 artist_id INTEGER PRIMARY KEY NOT NULL REFERENCES artists(id),
 score REAL, thumbnail TEXT, found_at REAL, last_shown REAL, first_shown REAL,
 offered_count INTEGER NOT NULL DEFAULT 0 CHECK(offered_count>=0),
 legacy_offered_count INTEGER NOT NULL DEFAULT 0 CHECK(legacy_offered_count>=0),
 legacy_first_shown REAL, legacy_last_shown REAL,
 unresolved_genre TEXT, genre_key TEXT REFERENCES genre_catalog(key),
 recovery_filename TEXT, recovery_digest TEXT, recovery_bytes INTEGER,
 CHECK(genre_key IS NULL OR unresolved_genre IS NULL),
 CHECK((recovery_filename IS NULL AND recovery_digest IS NULL AND recovery_bytes IS NULL)
    OR (recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL AND recovery_bytes IS NOT NULL
        AND length(recovery_digest)=64 AND recovery_bytes>0))
);
CREATE TABLE discovery_origins (
 id INTEGER PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('album','radio','genre','artist','artist_start',
     'discovery','spotify_export','spotify_model','legacy_unknown')),
 account_id INTEGER REFERENCES identities(id), album_id INTEGER REFERENCES albums(id),
 seed_video_id TEXT REFERENCES youtube_items(video_id), genre_key TEXT REFERENCES genre_catalog(key),
 artist_id INTEGER REFERENCES artists(id), import_key TEXT,
 legacy_source_key TEXT, resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved','legacy_account')),
 resolution_reason TEXT,
 CHECK((album_id IS NOT NULL)=(kind='album')),
 CHECK((seed_video_id IS NOT NULL)=(kind='radio')),
 CHECK((genre_key IS NOT NULL)=(kind='genre')),
 CHECK((artist_id IS NOT NULL)=(kind IN ('artist','artist_start'))),
 CHECK((import_key IS NOT NULL)=(kind='spotify_export' AND resolution_status='resolved')),
 CHECK(import_key IS NULL OR length(import_key)>0),
 CHECK(kind<>'spotify_export' OR account_id IS NOT NULL),
 CHECK((resolution_status='legacy_account')=(kind='spotify_export' AND import_key IS NULL)),
 CHECK(account_id IS NULL OR kind IN ('spotify_export','spotify_model','legacy_unknown')),
 CHECK((legacy_source_key IS NOT NULL)=(kind='legacy_unknown')),
 CHECK((resolution_status='unresolved')=(kind='legacy_unknown')),
 CHECK((resolution_reason IS NOT NULL)=(kind='legacy_unknown')),
 CHECK(kind<>'legacy_unknown' OR (length(legacy_source_key)>0 AND length(resolution_reason)>0))
);
CREATE UNIQUE INDEX ix_discovery_origin_album ON discovery_origins(album_id) WHERE kind='album';
CREATE UNIQUE INDEX ix_discovery_origin_radio ON discovery_origins(seed_video_id) WHERE kind='radio';
CREATE UNIQUE INDEX ix_discovery_origin_genre ON discovery_origins(genre_key) WHERE kind='genre';
CREATE UNIQUE INDEX ix_discovery_origin_artist ON discovery_origins(kind,artist_id) WHERE kind IN ('artist','artist_start');
CREATE UNIQUE INDEX ix_discovery_origin_import ON discovery_origins(account_id,coalesce(import_key,'')) WHERE kind='spotify_export';
CREATE UNIQUE INDEX ix_discovery_origin_independent ON discovery_origins(kind,coalesce(account_id,-1)) WHERE kind IN ('discovery','spotify_model');
CREATE UNIQUE INDEX ix_discovery_origin_unknown ON discovery_origins(legacy_source_key,coalesce(account_id,-1)) WHERE kind='legacy_unknown';
CREATE TABLE discovery_sources (
 id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL REFERENCES recordings(id),
 origin_id INTEGER NOT NULL REFERENCES discovery_origins(id),
 input_video_id TEXT REFERENCES youtube_items(video_id), first_found_at REAL, last_found_at REAL
);
CREATE UNIQUE INDEX ix_discovery_source_upload ON discovery_sources(recording_id,origin_id,input_video_id) WHERE input_video_id IS NOT NULL;
CREATE UNIQUE INDEX ix_discovery_source_unknown ON discovery_sources(recording_id,origin_id) WHERE input_video_id IS NULL;
CREATE INDEX ix_discovery_source_origin ON discovery_sources(origin_id,recording_id);
CREATE TABLE discovery_artist_sources (
 artist_id INTEGER NOT NULL REFERENCES artists(id), origin_id INTEGER NOT NULL REFERENCES discovery_origins(id),
 score REAL NOT NULL, PRIMARY KEY(artist_id,origin_id)
) WITHOUT ROWID;
CREATE INDEX ix_discovery_artist_origin ON discovery_artist_sources(origin_id,artist_id);
CREATE TABLE discovery_impressions (
 id INTEGER PRIMARY KEY, view_id TEXT NOT NULL,
 recording_id INTEGER REFERENCES recordings(id), artist_id INTEGER REFERENCES artists(id),
 album_id INTEGER REFERENCES albums(id), shown_at REAL NOT NULL,
 CHECK((recording_id IS NOT NULL)+(artist_id IS NOT NULL)+(album_id IS NOT NULL)=1)
);
CREATE UNIQUE INDEX ix_discovery_view_recording ON discovery_impressions(view_id,recording_id) WHERE recording_id IS NOT NULL;
CREATE UNIQUE INDEX ix_discovery_view_artist ON discovery_impressions(view_id,artist_id) WHERE artist_id IS NOT NULL;
CREATE UNIQUE INDEX ix_discovery_view_album ON discovery_impressions(view_id,album_id) WHERE album_id IS NOT NULL;
CREATE INDEX ix_discovery_impression_recording ON discovery_impressions(recording_id,shown_at) WHERE recording_id IS NOT NULL;
CREATE INDEX ix_discovery_impression_artist ON discovery_impressions(artist_id,shown_at) WHERE artist_id IS NOT NULL;
CREATE INDEX ix_discovery_impression_album ON discovery_impressions(album_id,shown_at) WHERE album_id IS NOT NULL;
CREATE TABLE unresolved_discovery_impressions (
 view_id TEXT NOT NULL, original_kind TEXT NOT NULL, original_key TEXT NOT NULL, shown_at REAL NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('missing_owner','ambiguous_owner','unsupported_kind')),
 PRIMARY KEY(view_id,original_kind,original_key)
) WITHOUT ROWID;
CREATE TABLE discovery_legacy_offers (
 id INTEGER PRIMARY KEY, recording_id INTEGER REFERENCES recordings(id),
 artist_id INTEGER REFERENCES artists(id), album_id INTEGER REFERENCES albums(id),
 offered_count INTEGER NOT NULL CHECK(offered_count>=0), first_shown REAL, last_shown REAL,
 CHECK((recording_id IS NOT NULL)+(artist_id IS NOT NULL)+(album_id IS NOT NULL)=1)
);
CREATE UNIQUE INDEX ix_discovery_legacy_recording ON discovery_legacy_offers(recording_id) WHERE recording_id IS NOT NULL;
CREATE UNIQUE INDEX ix_discovery_legacy_artist ON discovery_legacy_offers(artist_id) WHERE artist_id IS NOT NULL;
CREATE UNIQUE INDEX ix_discovery_legacy_album ON discovery_legacy_offers(album_id) WHERE album_id IS NOT NULL;
CREATE VIEW discovery_origin_tokens AS
 SELECT o.id,o.kind,o.account_id,
 CASE o.kind
   WHEN 'album' THEN (SELECT provider_id FROM album_primary_provider_ids WHERE album_id=o.album_id AND provider='youtube')
   WHEN 'radio' THEN 'radio:'||o.seed_video_id
   WHEN 'genre' THEN 'genre:'||o.genre_key
   WHEN 'artist' THEN 'artist:'||(SELECT min(provider_id) FROM artist_provider_ids WHERE artist_id=o.artist_id AND provider='youtube')
   WHEN 'artist_start' THEN 'artist_start:'||(SELECT min(provider_id) FROM artist_provider_ids WHERE artist_id=o.artist_id AND provider='youtube')
   WHEN 'spotify_export' THEN 'spotify_export:'||o.account_id
   WHEN 'legacy_unknown' THEN o.legacy_source_key
   ELSE o.kind END source_token
 FROM discovery_origins o;
