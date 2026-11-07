CREATE TABLE sync_attempts (
 id INTEGER PRIMARY KEY CHECK(id=1),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 phase TEXT NOT NULL CHECK(phase IN ('running','complete','partial','failed','skipped')),
 outcome TEXT CHECK(outcome IN ('complete','partial','failed','skipped')),
 started_at REAL CHECK(started_at IS NULL OR (typeof(started_at) IN ('integer','real') AND abs(started_at)<=1.7976931348623157e308)),
 finished_at REAL CHECK(finished_at IS NULL OR (typeof(finished_at) IN ('integer','real') AND abs(finished_at)<=1.7976931348623157e308)),
 last_success_at REAL CHECK(last_success_at IS NULL OR (typeof(last_success_at) IN ('integer','real') AND abs(last_success_at)<=1.7976931348623157e308)),
 recovery_filename TEXT,
 recovery_digest TEXT CHECK(recovery_digest IS NULL OR length(recovery_digest)=64),
 recovery_bytes INTEGER CHECK(recovery_bytes IS NULL OR (typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)),
 CHECK((recovery_filename IS NULL AND recovery_digest IS NULL AND recovery_bytes IS NULL) OR (recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL AND recovery_bytes IS NOT NULL))
);
CREATE TABLE sync_cursors (
 id INTEGER PRIMARY KEY,
 account_id INTEGER REFERENCES identities(id),
 provider TEXT NOT NULL CHECK(provider='youtube'),
 collection TEXT NOT NULL CHECK(collection IN ('account','playlist_listing','playlists','ratings','history','saved_albums','saved_album_mirror','album_tracks','playlist')),
 playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 cursor TEXT,
 phase TEXT NOT NULL CHECK(phase IN ('complete','partial','failed','skipped')),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
 last_success_at REAL CHECK(last_success_at IS NULL OR (typeof(last_success_at) IN ('integer','real') AND abs(last_success_at)<=1.7976931348623157e308)),
 failures INTEGER CHECK(failures IS NULL OR (typeof(failures)='integer' AND failures BETWEEN 0 AND 16)),
 retry_at REAL CHECK(retry_at IS NULL OR (typeof(retry_at) IN ('integer','real') AND abs(retry_at)<=1.7976931348623157e308)),
 CHECK((collection='playlist' AND playlist_scope_id IS NOT NULL AND account_id IS NOT NULL) OR (collection<>'playlist' AND playlist_scope_id IS NULL)),
 CHECK(account_id IS NOT NULL OR collection IN ('saved_album_mirror','album_tracks'))
);
CREATE UNIQUE INDEX ix_sync_account_resource ON sync_cursors(account_id,provider,collection) WHERE account_id IS NOT NULL AND playlist_scope_id IS NULL;
CREATE UNIQUE INDEX ix_sync_global_resource ON sync_cursors(provider,collection) WHERE account_id IS NULL;
CREATE UNIQUE INDEX ix_sync_playlist_resource ON sync_cursors(account_id,provider,playlist_scope_id) WHERE playlist_scope_id IS NOT NULL;
CREATE INDEX ix_sync_retry ON sync_cursors(phase,retry_at,account_id);
CREATE TRIGGER sync_cursor_scope_insert BEFORE INSERT ON sync_cursors
WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_scopes s
 WHERE s.ref=NEW.playlist_scope_id AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Sync playlist belongs to another account'); END;
CREATE TRIGGER sync_cursor_scope_update BEFORE UPDATE ON sync_cursors
WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_scopes s
 WHERE s.ref=NEW.playlist_scope_id AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'Sync playlist belongs to another account'); END;
