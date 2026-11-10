CREATE INDEX ix_album_current_tracklist ON albums(current_tracklist_id);
CREATE TABLE album_tracklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
    capture_kind TEXT NOT NULL CHECK(capture_kind IN ('provider','legacy_full','legacy_share','share')),
    status TEXT NOT NULL CHECK(status IN ('building','partial','complete','snapshot')),
    order_status TEXT NOT NULL CHECK(order_status IN ('known','unknown','mixed')),
    expected_count INTEGER CHECK(expected_count IS NULL OR (typeof(expected_count)='integer' AND expected_count>=0)),
    fetched_at REAL CHECK(fetched_at IS NULL OR (typeof(fetched_at) IN ('integer','real') AND fetched_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),
    CHECK((status<>'snapshot' OR capture_kind IN ('legacy_share','share')) AND (status<>'complete' OR capture_kind IN ('provider','legacy_full'))),
    FOREIGN KEY(provider,provider_id) REFERENCES album_provider_ids(provider,provider_id),
    UNIQUE(provider,provider_id,revision),
    UNIQUE(id,album_id)
);
CREATE INDEX ix_album_tracklist_owner ON album_tracklists(album_id,id);
CREATE TABLE album_track_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracklist_id INTEGER NOT NULL REFERENCES album_tracklists(id),
    position INTEGER CHECK(position IS NULL OR (typeof(position)='integer' AND position>=0)),
    disc_number INTEGER CHECK(disc_number IS NULL OR (typeof(disc_number)='integer' AND disc_number>0)),
    track_number INTEGER CHECK(track_number IS NULL OR (typeof(track_number)='integer' AND track_number>0)),
    recording_id INTEGER REFERENCES recordings(id),
    captured_title TEXT,
    captured_credit TEXT,
    raw_identity_key TEXT,
    raw_source_ref TEXT,
    captured_source TEXT,
    resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved','unavailable')),
    legacy_row_id INTEGER UNIQUE,
    CHECK((recording_id IS NOT NULL AND resolution_status='resolved') OR (recording_id IS NULL AND resolution_status IN ('unresolved','unavailable'))),
    UNIQUE(tracklist_id,position),
    UNIQUE(id,tracklist_id)
);
CREATE INDEX ix_album_occurrence_recording ON album_track_occurrences(recording_id,tracklist_id);
CREATE TABLE album_occurrence_sources (
    occurrence_id INTEGER NOT NULL REFERENCES album_track_occurrences(id),
    entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
    provider TEXT NOT NULL CHECK(provider IN ('youtube','local','unresolved')),
    PRIMARY KEY(occurrence_id,entry_id,provider)
);
CREATE INDEX ix_album_occurrence_entry ON album_occurrence_sources(entry_id,occurrence_id);
CREATE TABLE album_item_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    entry_id INTEGER REFERENCES catalog_entries(id),
    provider TEXT NOT NULL CHECK(length(trim(provider))>0),
    first_seen REAL,
    last_seen REAL,
    recording_id INTEGER REFERENCES recordings(id),
    raw_identity_key TEXT,
    legacy_row_id INTEGER UNIQUE,
    captured_position INTEGER,
    captured_disc_number INTEGER,
    captured_track_number INTEGER,
    CHECK(entry_id IS NOT NULL OR recording_id IS NOT NULL OR raw_identity_key IS NOT NULL),
    CHECK(first_seen IS NULL OR last_seen IS NULL OR first_seen<=last_seen)
);
CREATE UNIQUE INDEX ix_album_item_known ON album_item_observations(album_id,entry_id,provider) WHERE entry_id IS NOT NULL;
CREATE INDEX ix_album_item_entry ON album_item_observations(entry_id,album_id);
CREATE INDEX ix_album_item_recording ON album_item_observations(recording_id,album_id);
CREATE TABLE album_share_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    tracklist_id INTEGER NOT NULL,
    captured_provider_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at REAL,
    FOREIGN KEY(tracklist_id,album_id) REFERENCES album_tracklists(id,album_id) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE(id,tracklist_id)
);
CREATE INDEX ix_album_share_provider ON album_share_snapshots(captured_provider_id,id DESC);
CREATE INDEX ix_album_share_owner ON album_share_snapshots(album_id,id DESC);
CREATE INDEX ix_album_share_tracklist ON album_share_snapshots(tracklist_id,id);
CREATE TABLE album_share_items (
    snapshot_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
    tracklist_id INTEGER NOT NULL,
    occurrence_id INTEGER NOT NULL,
    entry_id INTEGER REFERENCES catalog_entries(id),
    raw_source_ref TEXT,
    CHECK(entry_id IS NOT NULL OR raw_source_ref IS NOT NULL),
    FOREIGN KEY(snapshot_id,tracklist_id) REFERENCES album_share_snapshots(id,tracklist_id),
    FOREIGN KEY(occurrence_id,tracklist_id) REFERENCES album_track_occurrences(id,tracklist_id),
    PRIMARY KEY(snapshot_id,position)
);
CREATE INDEX ix_album_share_occurrence ON album_share_items(occurrence_id);
CREATE VIEW album_tracks AS
    SELECT o.id,t.album_id,(SELECT min(s.entry_id) FROM album_occurrence_sources s WHERE s.occurrence_id=o.id) entry_id,
           CASE WHEN EXISTS(SELECT 1 FROM album_occurrence_sources s WHERE s.occurrence_id=o.id) THEN NULL ELSE o.recording_id END recording_id,
           o.raw_identity_key legacy_track_key,o.position,o.disc_number,o.track_number,1 is_full,coalesce(o.captured_source,'full_fetch') source
    FROM albums a JOIN album_tracklists t ON t.id=a.current_tracklist_id JOIN album_track_occurrences o ON o.tracklist_id=t.id
    UNION ALL
    SELECT -i.id,i.album_id,i.entry_id,i.recording_id,i.raw_identity_key,i.captured_position,i.captured_disc_number,i.captured_track_number,0,i.provider
    FROM album_item_observations i WHERE NOT EXISTS(
        SELECT 1 FROM albums a JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id
        JOIN album_occurrence_sources s ON s.occurrence_id=o.id WHERE a.id=i.album_id AND s.entry_id=i.entry_id);
CREATE VIEW album_fetch_state AS
    SELECT a.id album_id,t.fetched_at,1 complete,t.expected_count expected_track_count
    FROM albums a JOIN album_tracklists t ON t.id=a.current_tracklist_id WHERE t.status='complete';
CREATE VIEW album_track_links AS
    SELECT min(t.id) id,p.provider_id browse_id,e.recording_id,t.entry_id,e.video_id,min(t.source) source
    FROM album_tracks t JOIN album_provider_ids p ON p.album_id=t.album_id AND p.provider='youtube'
    JOIN catalog_entries e ON e.id=t.entry_id GROUP BY p.provider_id,t.entry_id;
CREATE VIEW album_tracklist_keys AS
    SELECT p.provider_id browse_id,a.title,a.artist,a.thumbnail,
        (SELECT json_group_array(k) FROM (SELECT coalesce(r.identity_key,o.raw_identity_key) k
         FROM album_track_occurrences o LEFT JOIN recordings r ON r.id=o.recording_id
         WHERE o.tracklist_id=t.id ORDER BY o.position IS NULL,o.position,o.id)) track_keys
    FROM albums a JOIN album_tracklists t ON t.id=a.current_tracklist_id
    JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' WHERE t.status='complete';
