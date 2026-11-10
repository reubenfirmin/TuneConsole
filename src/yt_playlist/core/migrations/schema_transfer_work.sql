CREATE TABLE playlist_gc_attempts (
 id INTEGER PRIMARY KEY,
 captured_account_id INTEGER NOT NULL CHECK(typeof(captured_account_id)='integer'),
 account_id INTEGER REFERENCES identities(id),
 playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 provider_playlist_id_snapshot TEXT NOT NULL CHECK(length(provider_playlist_id_snapshot)>0),
 revision TEXT NOT NULL CHECK(length(revision)=32),
 attempts INTEGER NOT NULL CHECK(typeof(attempts)='integer' AND attempts>=1),
 last_attempt_at REAL NOT NULL CHECK(typeof(last_attempt_at) IN ('integer','real') AND abs(last_attempt_at)<=1.7976931348623157e308),
 next_retry_at REAL NOT NULL CHECK(typeof(next_retry_at) IN ('integer','real') AND abs(next_retry_at)<=1.7976931348623157e308),
 phase TEXT CHECK(phase IS NULL OR phase IN ('reconcile','backup','delete')),
 failure_class TEXT,
 error TEXT,
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 UNIQUE(captured_account_id,provider_playlist_id_snapshot),
 CHECK(account_id IS NULL OR account_id=captured_account_id)
);
CREATE INDEX ix_gc_attempt_deadline ON playlist_gc_attempts(next_retry_at);
CREATE TABLE spotify_transfer_receipts (
 id INTEGER PRIMARY KEY,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('playlist','album')),
 captured_account_id INTEGER CHECK(captured_account_id IS NULL OR typeof(captured_account_id)='integer'),
 account_id INTEGER REFERENCES identities(id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)>0),
 revision TEXT NOT NULL CHECK(length(revision)=32),
 creation_phase TEXT NOT NULL CHECK(creation_phase IN ('legacy','prepared','creating','created')),
 marker TEXT,
 playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 provider_playlist_id_snapshot TEXT,
 complete INTEGER CHECK(complete IS NULL OR (typeof(complete)='integer' AND complete IN (0,1))),
 order_verified INTEGER CHECK(order_verified IS NULL OR (typeof(order_verified)='integer' AND order_verified IN (0,1))),
 updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
 last_run_added INTEGER CHECK(last_run_added IS NULL OR (typeof(last_run_added)='integer' AND last_run_added>=0)),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 CHECK(account_id IS NULL OR account_id=captured_account_id),
 CHECK(creation_phase<>'creating' OR marker IS NOT NULL),
 CHECK(creation_phase<>'created' OR provider_playlist_id_snapshot IS NOT NULL)
);
CREATE UNIQUE INDEX uq_spotify_scoped_receipt ON spotify_transfer_receipts(captured_account_id,source_kind,fingerprint)
 WHERE captured_account_id IS NOT NULL;
CREATE UNIQUE INDEX uq_spotify_legacy_receipt ON spotify_transfer_receipts(source_kind,fingerprint)
 WHERE captured_account_id IS NULL;
CREATE TABLE spotify_transfer_items (
 receipt_id INTEGER NOT NULL REFERENCES spotify_transfer_receipts(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 video_id_snapshot TEXT CHECK(video_id_snapshot IS NULL OR length(video_id_snapshot)>0),
 PRIMARY KEY(receipt_id,position)
) WITHOUT ROWID;
CREATE TABLE tv_append_intents (
 id INTEGER PRIMARY KEY,
 recipe_id INTEGER REFERENCES rec_recipes(id) ON DELETE SET NULL,
 playlist_scope_id TEXT NOT NULL REFERENCES playlist_scopes(ref),
 stream_token TEXT NOT NULL,
 request_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','complete')),
 started_at REAL CHECK(started_at IS NULL OR (typeof(started_at) IN ('integer','real') AND abs(started_at)<=1.7976931348623157e308)),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 UNIQUE(playlist_scope_id,stream_token,request_id)
);
CREATE INDEX ix_tv_append_pending ON tv_append_intents(state,playlist_scope_id);
CREATE TABLE tv_append_items (
 intent_id INTEGER NOT NULL REFERENCES tv_append_intents(id),
 role TEXT NOT NULL CHECK(role IN ('before','additions','items')),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),
 PRIMARY KEY(intent_id,role,position)
) WITHOUT ROWID;
CREATE TABLE radio_operations (
 session_id TEXT PRIMARY KEY REFERENCES radio_sessions(id),
 session_revision INTEGER NOT NULL CHECK(typeof(session_revision)='integer' AND session_revision>=1),
 kind TEXT NOT NULL CHECK(kind IN ('append','replacement')),
 sent INTEGER NOT NULL CHECK(typeof(sent)='integer' AND sent IN (0,1)),
 started_at REAL CHECK(started_at IS NULL OR (typeof(started_at) IN ('integer','real') AND abs(started_at)<=1.7976931348623157e308)),
 receipt_status TEXT,
 receipt_video_id_snapshot TEXT,
 receipt_handle_snapshot TEXT,
 allowed_count INTEGER NOT NULL CHECK(typeof(allowed_count)='integer' AND allowed_count>=0),
 waiting_present INTEGER NOT NULL CHECK(typeof(waiting_present)='integer' AND waiting_present IN (0,1)),
 play_prefix_present INTEGER NOT NULL CHECK(typeof(play_prefix_present)='integer' AND play_prefix_present IN (0,1)),
 adding_video_id_snapshot TEXT,
 slot INTEGER CHECK(slot IS NULL OR (typeof(slot)='integer' AND slot>=0)),
 choice_id TEXT
);
CREATE TABLE radio_operation_items (
 session_id TEXT NOT NULL REFERENCES radio_operations(session_id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('allowed','confirmed','waiting','play_prefix','proposed','excluded')),
 list_position INTEGER NOT NULL CHECK(typeof(list_position)='integer' AND list_position>=0),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),
 PRIMARY KEY(session_id,role,list_position,position)
) WITHOUT ROWID;
