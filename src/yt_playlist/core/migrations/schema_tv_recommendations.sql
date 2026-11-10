CREATE TABLE tv_pool_state (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 255),
    schema_version INTEGER CHECK(schema_version IS NULL OR (typeof(schema_version)='integer' AND schema_version>=0)),
    cache_key TEXT,
    at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
    offset INTEGER CHECK(offset IS NULL OR (typeof(offset)='integer' AND offset>=0)),
    state TEXT CHECK(state IS NULL OR state IN ('searching','exhausted','no_sources')),
    retry_at REAL CHECK(retry_at IS NULL OR (typeof(retry_at) IN ('integer','real') AND abs(retry_at)<=1.7976931348623157e308))
);
CREATE TABLE tv_pool_items (
    pool_id INTEGER NOT NULL REFERENCES tv_pool_state(id),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 59),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 2047),
    video_id TEXT NOT NULL CHECK(length(video_id)=11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    title TEXT,
    artist TEXT,
    thumbnail TEXT,
    duration INTEGER CHECK(duration IS NULL OR (typeof(duration)='integer' AND duration>0)),
    kind TEXT,
    source_key TEXT,
    source_artist TEXT,
    source_group TEXT CHECK(source_group IS NULL OR source_group IN ('liked','your_artists','discovery')),
    connection TEXT,
    key TEXT CHECK(key IS NULL OR key=video_id),
    source_recording_id INTEGER REFERENCES recordings(id),
    source_resolution TEXT,
    recording_id INTEGER REFERENCES recordings(id),
    video_resolution TEXT,
    CHECK((source_recording_id IS NOT NULL AND source_resolution IS NULL) OR (source_recording_id IS NULL AND source_resolution IS NOT NULL AND source_resolution='missing_recording')),
    CHECK((recording_id IS NOT NULL AND video_resolution IS NULL) OR (recording_id IS NULL AND video_resolution IS NOT NULL AND video_resolution='external_video')),
    PRIMARY KEY(pool_id,position),
    UNIQUE(pool_id,video_id)
) WITHOUT ROWID;
CREATE TABLE tv_pool_attempts (
    pool_id INTEGER NOT NULL REFERENCES tv_pool_state(id),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 59),
    raw_key TEXT NOT NULL CHECK(length(raw_key)>0),
    recording_id INTEGER REFERENCES recordings(id),
    resolution_reason TEXT,
    CHECK((recording_id IS NOT NULL AND resolution_reason IS NULL) OR (recording_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_recording')),
    PRIMARY KEY(pool_id,position),
    UNIQUE(pool_id,raw_key)
) WITHOUT ROWID;
CREATE TABLE tv_artwork_state (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308))
);
CREATE TABLE tv_artist_artwork (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    artist_key TEXT NOT NULL UNIQUE CHECK(length(artist_key)>0),
    artist_id INTEGER REFERENCES artists(id),
    resolution_reason TEXT,
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 7),
    retry_at REAL CHECK(retry_at IS NULL OR (typeof(retry_at) IN ('integer','real') AND abs(retry_at)<=1.7976931348623157e308)),
    at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
    CHECK((artist_id IS NOT NULL AND resolution_reason IS NULL) OR (artist_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_artist'))
);
CREATE TABLE tv_artist_images (
    artwork_id INTEGER NOT NULL REFERENCES tv_artist_artwork(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 5),
    url TEXT NOT NULL CHECK(length(url)>0),
    PRIMARY KEY(artwork_id,position),
    UNIQUE(artwork_id,url)
) WITHOUT ROWID;
CREATE TABLE tv_artwork_request_state (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308))
);
CREATE TABLE tv_artist_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    artist_key TEXT NOT NULL UNIQUE CHECK(length(artist_key)>0),
    display_name TEXT NOT NULL CHECK(length(display_name)>0),
    artist_id INTEGER REFERENCES artists(id),
    requested_at REAL CHECK(requested_at IS NULL OR (typeof(requested_at) IN ('integer','real') AND abs(requested_at)<=1.7976931348623157e308)),
    resolution_reason TEXT,
    CHECK((artist_id IS NOT NULL AND resolution_reason IS NULL) OR (artist_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_artist'))
);
CREATE TABLE tv_visual_state (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308))
);
CREATE TABLE tv_visual_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    video_id TEXT NOT NULL UNIQUE CHECK(length(video_id)=11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 127),
    verdict TEXT CHECK(verdict IS NULL OR verdict IN ('static','moving','unknown')),
    version INTEGER CHECK(version IS NULL OR (typeof(version)='integer' AND version>=0)),
    at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
    retry_at REAL CHECK(retry_at IS NULL OR (typeof(retry_at) IN ('integer','real') AND abs(retry_at)<=1.7976931348623157e308)),
    difference REAL CHECK(difference IS NULL OR (typeof(difference) IN ('integer','real') AND difference>=0 AND difference<=1.7976931348623157e308)),
    changed_fraction REAL CHECK(changed_fraction IS NULL OR changed_fraction BETWEEN 0 AND 1)
);
CREATE INDEX ix_tv_visual_verdict ON tv_visual_results(version,verdict,video_id);
CREATE TABLE tv_visual_samples (
    result_id INTEGER NOT NULL REFERENCES tv_visual_results(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 1),
    at REAL NOT NULL CHECK(typeof(at) IN ('integer','real') AND at>=0 AND at<=1.7976931348623157e308),
    PRIMARY KEY(result_id,position)
) WITHOUT ROWID;
CREATE TABLE tv_continuation_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    request_key TEXT NOT NULL UNIQUE CHECK(length(request_key)>0),
    source_playlist_id INTEGER NOT NULL CHECK(typeof(source_playlist_id)='integer' AND source_playlist_id>0),
    source_ref INTEGER REFERENCES playlist_membership_refs(id),
    source_resolution TEXT,
    input_stream TEXT NOT NULL,
    request_id TEXT NOT NULL CHECK(length(request_id)>0),
    status TEXT NOT NULL CHECK(status IN ('pending','complete')),
    target_playlist_id INTEGER CHECK(target_playlist_id IS NULL OR (typeof(target_playlist_id)='integer' AND target_playlist_id>0)),
    target_ref INTEGER REFERENCES playlist_membership_refs(id),
    target_resolution TEXT,
    target_stream TEXT NOT NULL CHECK(length(target_stream)>0),
    created_at REAL CHECK(created_at IS NULL OR (typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308)),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    CHECK((source_ref IS NOT NULL AND source_ref=source_playlist_id AND source_resolution IS NULL) OR (source_ref IS NULL AND source_resolution IS NOT NULL AND source_resolution='missing_playlist')),
    CHECK((status='pending' AND target_playlist_id IS NULL AND target_ref IS NULL AND target_resolution IS NULL) OR
          (status='complete' AND target_playlist_id IS NOT NULL AND ((target_ref IS NOT NULL AND target_ref=target_playlist_id AND target_resolution IS NULL) OR
           (target_ref IS NULL AND target_resolution IS NOT NULL AND target_resolution='missing_playlist')))),
    UNIQUE(source_playlist_id,input_stream,request_id)
);
CREATE INDEX ix_tv_continuations_status ON tv_continuation_receipts(status,id);
CREATE TABLE tv_continuation_items (
    receipt_id INTEGER NOT NULL REFERENCES tv_continuation_receipts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 59),
    field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 2047),
    video_id TEXT NOT NULL CHECK(length(video_id)=11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    title TEXT,
    artist TEXT,
    thumbnail TEXT,
    duration INTEGER CHECK(duration IS NULL OR (typeof(duration)='integer' AND duration>0)),
    kind TEXT,
    source_key TEXT,
    source_artist TEXT,
    source_group TEXT CHECK(source_group IS NULL OR source_group IN ('liked','your_artists','discovery')),
    connection TEXT,
    key TEXT CHECK(key IS NULL OR key=video_id),
    source_recording_id INTEGER REFERENCES recordings(id),
    source_resolution TEXT,
    recording_id INTEGER REFERENCES recordings(id),
    video_resolution TEXT,
    CHECK((source_recording_id IS NOT NULL AND source_resolution IS NULL) OR (source_recording_id IS NULL AND source_resolution IS NOT NULL AND source_resolution='missing_recording')),
    CHECK((recording_id IS NOT NULL AND video_resolution IS NULL) OR (recording_id IS NULL AND video_resolution IS NOT NULL AND video_resolution='external_video')),
    PRIMARY KEY(receipt_id,position),
    UNIQUE(receipt_id,video_id)
) WITHOUT ROWID;
CREATE TRIGGER tv_receipt_complete_update BEFORE UPDATE ON tv_continuation_receipts WHEN OLD.status='complete'
BEGIN SELECT RAISE(ABORT,'Completed TV receipt is immutable'); END;
CREATE TRIGGER tv_receipt_pending_delete BEFORE DELETE ON tv_continuation_receipts WHEN OLD.status='pending'
BEGIN SELECT RAISE(ABORT,'Unconfirmed TV creation must be retained'); END;
