CREATE TABLE account_auth_expirations (
 id INTEGER PRIMARY KEY,
 account_id INTEGER REFERENCES identities(id),
 unresolved_account_id INTEGER CHECK(unresolved_account_id IS NULL OR (typeof(unresolved_account_id)='integer' AND unresolved_account_id>0)),
 label TEXT NOT NULL,
 CHECK((account_id IS NOT NULL AND unresolved_account_id IS NULL) OR (account_id IS NULL AND unresolved_account_id IS NOT NULL))
);
CREATE UNIQUE INDEX ix_auth_expiration_account ON account_auth_expirations(account_id) WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX ix_auth_expiration_unresolved ON account_auth_expirations(unresolved_account_id) WHERE unresolved_account_id IS NOT NULL;
CREATE TABLE album_feedback (
 id INTEGER PRIMARY KEY,
 account_id INTEGER REFERENCES identities(id),
 album_id INTEGER NOT NULL REFERENCES albums(id),
 source_browse_id TEXT NOT NULL,
 response TEXT NOT NULL CHECK(response IN ('more','known','reject')),
 created_at REAL NOT NULL CHECK(typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308),
 withdrawn_at REAL CHECK(withdrawn_at IS NULL OR (typeof(withdrawn_at) IN ('integer','real') AND abs(withdrawn_at)<=1.7976931348623157e308)),
 opportunity_id TEXT,
 action_id TEXT,
 legacy_author_unknown INTEGER NOT NULL DEFAULT 0 CHECK(legacy_author_unknown IN (0,1)),
 CHECK((account_id IS NOT NULL AND legacy_author_unknown=0) OR (account_id IS NULL AND legacy_author_unknown=1)),
 CHECK(withdrawn_at IS NULL OR withdrawn_at>=created_at)
);
CREATE UNIQUE INDEX ix_album_feedback_current ON album_feedback(account_id,album_id) WHERE withdrawn_at IS NULL AND account_id IS NOT NULL;
CREATE UNIQUE INDEX ix_album_feedback_unscoped ON album_feedback(album_id) WHERE withdrawn_at IS NULL AND account_id IS NULL;
CREATE INDEX ix_album_feedback_album ON album_feedback(album_id,withdrawn_at,account_id);
CREATE TABLE album_feedback_legacy_dismissals (
 account_id INTEGER NOT NULL REFERENCES identities(id),
 album_id INTEGER NOT NULL REFERENCES albums(id),
 dismissed_at REAL NOT NULL CHECK(typeof(dismissed_at) IN ('integer','real') AND abs(dismissed_at)<=1.7976931348623157e308),
 PRIMARY KEY(account_id,album_id)
) WITHOUT ROWID;
CREATE TABLE discovery_artist_image_state (
 artist_id INTEGER PRIMARY KEY REFERENCES artists(id),
 retry_at REAL NOT NULL CHECK(typeof(retry_at) IN ('integer','real') AND abs(retry_at)<=1.7976931348623157e308)
);
CREATE TABLE discovery_fetch_history (
 source_type TEXT NOT NULL CHECK(source_type IN ('album','artist','genre','radio')),
 source_id TEXT NOT NULL CHECK(length(source_id)>0),
 album_id INTEGER REFERENCES albums(id),
 PRIMARY KEY(source_type,source_id),
 CHECK((source_type='album' AND album_id IS NOT NULL) OR (source_type<>'album' AND album_id IS NULL))
) WITHOUT ROWID;
CREATE TABLE provider_refresh_frontiers (
 provider TEXT PRIMARY KEY NOT NULL REFERENCES enrichment_provider_config(provider),
 cursor INTEGER NOT NULL CHECK(typeof(cursor)='integer' AND cursor>=0),
 cutoff INTEGER NOT NULL CHECK(typeof(cutoff)='integer' AND cutoff>=0),
 fingerprint TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1)
) WITHOUT ROWID;
CREATE TABLE road_trip_drafts (
 id TEXT PRIMARY KEY NOT NULL,
 recipe_id INTEGER UNIQUE REFERENCES road_trip_recipes(id) ON DELETE CASCADE,
 account_id INTEGER REFERENCES identities(id),
 legacy_author_unknown INTEGER NOT NULL DEFAULT 0 CHECK(legacy_author_unknown IN (0,1)),
 revision TEXT NOT NULL CHECK(length(revision)>0),
 state TEXT NOT NULL CHECK(state IN ('building','ready','saved','failed')),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 updated_at REAL NOT NULL CHECK(typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308),
 CHECK((account_id IS NOT NULL AND legacy_author_unknown=0) OR (account_id IS NULL AND legacy_author_unknown=1))
);
CREATE TABLE road_trip_draft_entries (
 draft_id TEXT NOT NULL REFERENCES road_trip_drafts(id) ON DELETE CASCADE,
 selection_state TEXT NOT NULL CHECK(selection_state IN ('picked','excluded')),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 recording_id INTEGER NOT NULL REFERENCES recordings(id),
 entry_id INTEGER REFERENCES catalog_entries(id),
 video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
 PRIMARY KEY(draft_id,selection_state,position)
) WITHOUT ROWID;
CREATE INDEX ix_draft_entry_recording ON road_trip_draft_entries(recording_id,draft_id);
CREATE INDEX ix_draft_entry_source ON road_trip_draft_entries(entry_id);
CREATE TRIGGER draft_entry_subject_insert BEFORE INSERT ON road_trip_draft_entries
WHEN NOT EXISTS(SELECT 1 FROM youtube_items WHERE video_id=NEW.video_id AND recording_id=NEW.recording_id)
 OR (NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries WHERE id=NEW.entry_id AND recording_id=NEW.recording_id AND video_id=NEW.video_id))
BEGIN SELECT RAISE(ABORT,'Draft source belongs to another recording'); END;
CREATE TRIGGER draft_entry_subject_update BEFORE UPDATE ON road_trip_draft_entries
WHEN NOT EXISTS(SELECT 1 FROM youtube_items WHERE video_id=NEW.video_id AND recording_id=NEW.recording_id)
 OR (NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries WHERE id=NEW.entry_id AND recording_id=NEW.recording_id AND video_id=NEW.video_id))
BEGIN SELECT RAISE(ABORT,'Draft source belongs to another recording'); END;
