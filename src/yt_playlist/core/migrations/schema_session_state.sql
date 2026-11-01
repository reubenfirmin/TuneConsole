CREATE TABLE radio_playlist_origins (
 id INTEGER PRIMARY KEY,
 playlist_scope_id TEXT UNIQUE REFERENCES playlist_scopes(ref),
 unresolved_provider_id TEXT UNIQUE CHECK(unresolved_provider_id IS NULL OR length(unresolved_provider_id)>0),
 CHECK((playlist_scope_id IS NOT NULL AND unresolved_provider_id IS NULL)
    OR (playlist_scope_id IS NULL AND unresolved_provider_id IS NOT NULL))
);
CREATE TABLE radio_sessions (
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)>0),
 account_id INTEGER NOT NULL REFERENCES identities(id),
 playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 status TEXT NOT NULL CHECK(status IN ('preparing','awaiting_playback','playing','appending','replacing','conflict','stopping','stopped')),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 updated_at REAL NOT NULL CHECK(typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)
);
CREATE TABLE radio_session_head (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 session_id TEXT NOT NULL UNIQUE REFERENCES radio_sessions(id)
);
CREATE TABLE radio_queue_items (
 session_id TEXT NOT NULL REFERENCES radio_sessions(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 recording_id INTEGER NOT NULL REFERENCES recordings(id),
 entry_id INTEGER REFERENCES catalog_entries(id),
 video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
 state TEXT NOT NULL CHECK(state IN ('played','current','queued','initial','pending')),
 PRIMARY KEY(session_id,position)
) WITHOUT ROWID;
CREATE INDEX ix_radio_queue_recording ON radio_queue_items(recording_id,session_id);
CREATE TRIGGER radio_queue_subject_insert BEFORE INSERT ON radio_queue_items
WHEN NOT EXISTS(SELECT 1 FROM youtube_items WHERE video_id=NEW.video_id AND recording_id=NEW.recording_id)
 OR (NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries WHERE id=NEW.entry_id AND recording_id=NEW.recording_id AND video_id=NEW.video_id))
BEGIN SELECT RAISE(ABORT,'Radio source belongs to another recording'); END;
CREATE TRIGGER radio_queue_subject_update BEFORE UPDATE ON radio_queue_items
WHEN NOT EXISTS(SELECT 1 FROM youtube_items WHERE video_id=NEW.video_id AND recording_id=NEW.recording_id)
 OR (NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries WHERE id=NEW.entry_id AND recording_id=NEW.recording_id AND video_id=NEW.video_id))
BEGIN SELECT RAISE(ABORT,'Radio source belongs to another recording'); END;
CREATE TRIGGER radio_session_scope_insert BEFORE INSERT ON radio_sessions
WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_scopes s
 WHERE s.ref=NEW.playlist_scope_id AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Radio playlist belongs to another account'); END;
CREATE TRIGGER radio_session_scope_update BEFORE UPDATE ON radio_sessions
WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_scopes s
 WHERE s.ref=NEW.playlist_scope_id AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Radio playlist belongs to another account'); END;
CREATE TABLE album_playback_intents (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 account_id INTEGER REFERENCES identities(id),
 album_id INTEGER NOT NULL REFERENCES albums(id),
 source_browse_id TEXT NOT NULL,
 playlist_scope_id TEXT NOT NULL REFERENCES playlist_scopes(ref),
 opportunity_id TEXT NOT NULL,
 action_id TEXT,
 started_at REAL NOT NULL CHECK(typeof(started_at) IN ('integer','real') AND abs(started_at)<=1.7976931348623157e308),
 has_videos INTEGER NOT NULL CHECK(has_videos IN (0,1)),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1)
);
CREATE TABLE album_playback_intent_items (
 intent_id INTEGER NOT NULL REFERENCES album_playback_intents(singleton) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
 PRIMARY KEY(intent_id,position)
) WITHOUT ROWID;
CREATE TRIGGER album_playback_scope_insert BEFORE INSERT ON album_playback_intents
WHEN NOT EXISTS(SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id
 AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Album playback playlist belongs to another account'); END;
CREATE TRIGGER album_playback_scope_update BEFORE UPDATE ON album_playback_intents
WHEN NOT EXISTS(SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id
 AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Album playback playlist belongs to another account'); END;
CREATE TABLE listening_playback_bindings (
 account_id INTEGER NOT NULL REFERENCES identities(id),
 playlist_scope_id TEXT NOT NULL REFERENCES playlist_scopes(ref),
 video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
 opportunity_id TEXT NOT NULL,
 option_id TEXT NOT NULL,
 bound_at REAL NOT NULL CHECK(typeof(bound_at) IN ('integer','real') AND abs(bound_at)<=1.7976931348623157e308),
 PRIMARY KEY(account_id,playlist_scope_id,video_id)
) WITHOUT ROWID;
CREATE INDEX ix_playback_binding_time ON listening_playback_bindings(bound_at);
CREATE TRIGGER playback_binding_scope_insert BEFORE INSERT ON listening_playback_bindings
WHEN NOT EXISTS(SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id
 AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Playback playlist belongs to another account'); END;
CREATE TRIGGER playback_binding_scope_update BEFORE UPDATE ON listening_playback_bindings
WHEN NOT EXISTS(SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id
 AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Playback playlist belongs to another account'); END;
