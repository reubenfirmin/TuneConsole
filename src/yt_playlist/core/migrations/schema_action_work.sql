CREATE TABLE actions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(length(trim(kind))>0),
 status TEXT NOT NULL CHECK(status IN ('draft','planned','pending','executing','executed','failed','cancelled','delete_pending','delete_failed','undoing','undo_failed','undone')),
 revision TEXT NOT NULL CHECK(length(revision)=32),
 idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)>0),
 intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64 AND intent_digest NOT GLOB '*[^0-9a-f]*'),
 parent_action_id INTEGER REFERENCES actions(id),
 undo_of_action_id INTEGER REFERENCES actions(id),
 reviewed_draft_id INTEGER,
 reviewed_draft_revision TEXT,
 reviewed_draft_signature TEXT,
 execution_token TEXT CHECK(execution_token IS NULL OR length(execution_token)=32),
 execution_mode TEXT CHECK(execution_mode IS NULL OR execution_mode IN ('delete','merge','move','copy')),
 created_at REAL CHECK(created_at IS NULL OR (typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308)),
 executed_at REAL CHECK(executed_at IS NULL OR (typeof(executed_at) IN ('integer','real') AND abs(executed_at)<=1.7976931348623157e308)),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 CHECK(parent_action_id IS NULL OR parent_action_id<>id),
 CHECK(undo_of_action_id IS NULL OR undo_of_action_id<>id),
 CHECK((reviewed_draft_id IS NULL AND reviewed_draft_revision IS NULL AND reviewed_draft_signature IS NULL) OR
       (reviewed_draft_id IS NOT NULL AND reviewed_draft_revision IS NOT NULL AND reviewed_draft_signature IS NOT NULL))
);
CREATE INDEX ix_actions_status ON actions(kind,status,id);
CREATE UNIQUE INDEX uq_action_reviewed_revision ON actions(reviewed_draft_id,reviewed_draft_revision)
 WHERE reviewed_draft_id IS NOT NULL;
CREATE TABLE action_drafts (
 id INTEGER PRIMARY KEY REFERENCES actions(id) ON DELETE CASCADE,
 account_id INTEGER REFERENCES identities(id),
 signature TEXT NOT NULL UNIQUE,
 revision TEXT NOT NULL CHECK(length(revision)=32),
 updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
CREATE TABLE action_targets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 action_id INTEGER NOT NULL REFERENCES actions(id),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 role TEXT NOT NULL CHECK(role IN ('source','destination','keeper','deletion','recovery','placement')),
 account_id INTEGER REFERENCES identities(id),
 unresolved_account_id INTEGER,
 playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 provider_playlist_id_snapshot TEXT NOT NULL CHECK(length(provider_playlist_id_snapshot)>0),
 membership_ref_id INTEGER REFERENCES playlist_membership_refs(id),
 unresolved_membership_ref_id INTEGER,
 resolution_reason TEXT CHECK(resolution_reason IS NULL OR resolution_reason IN ('missing_account','missing_membership','unrecorded_account','missing_scope','membership_snapshot_mismatch')),
 UNIQUE(action_id,role,position),
 UNIQUE(id,action_id),
 UNIQUE(id,account_id,playlist_scope_id),
 CHECK(account_id IS NULL OR unresolved_account_id IS NULL),
 CHECK(membership_ref_id IS NULL OR unresolved_membership_ref_id IS NULL),
 CHECK(account_id IS NOT NULL OR resolution_reason IS NOT NULL)
);
CREATE INDEX ix_action_target_owner ON action_targets(account_id,provider_playlist_id_snapshot,action_id);
CREATE INDEX ix_action_target_scope ON action_targets(playlist_scope_id,account_id,action_id);
CREATE INDEX ix_action_target_membership ON action_targets(membership_ref_id,role,action_id);
CREATE TABLE action_steps (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 action_id INTEGER NOT NULL REFERENCES actions(id),
 target_id INTEGER,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 operation TEXT NOT NULL CHECK(operation IN ('delete_playlist','restore_backup','restore_keeper','delete_copy','place_tracks','merge_plan')),
 phase TEXT NOT NULL CHECK(phase IN ('prepared','pending','uncertain','failed','creating','created','restoring','deleting','remote_complete','complete','cancelled')),
 outcome TEXT NOT NULL CHECK(outcome IN ('not_sent','unknown','confirmed_present','confirmed_absent','complete','conflict')),
 claim_token TEXT CHECK(claim_token IS NULL OR length(claim_token)=32),
 creation_marker TEXT,
 created_provider_id_snapshot TEXT,
 created_playlist_scope_id TEXT REFERENCES playlist_scopes(ref),
 original_survived INTEGER CHECK(original_survived IS NULL OR (typeof(original_survived)='integer' AND original_survived IN (0,1))),
 observed_status TEXT NOT NULL CHECK(observed_status IN ('unrecorded','unknown','present')),
 backup_filename TEXT,
 backup_digest TEXT CHECK(backup_digest IS NULL OR (length(backup_digest)=64 AND backup_digest NOT GLOB '*[^0-9a-f]*')),
 backup_bytes INTEGER CHECK(backup_bytes IS NULL OR (typeof(backup_bytes)='integer' AND backup_bytes>0)),
 backup_status TEXT NOT NULL CHECK(backup_status IN ('none','verified','missing_historical')),
 updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
 FOREIGN KEY(target_id,action_id) REFERENCES action_targets(id,action_id),
 UNIQUE(action_id,position),
 CHECK((backup_status='none' AND backup_filename IS NULL AND backup_digest IS NULL AND backup_bytes IS NULL) OR
       (backup_status='verified' AND backup_filename IS NOT NULL AND backup_digest IS NOT NULL AND backup_bytes IS NOT NULL) OR
       (backup_status='missing_historical' AND backup_filename IS NOT NULL AND backup_digest IS NULL AND backup_bytes IS NULL)),
 CHECK(phase<>'uncertain' OR outcome='unknown'),
 CHECK(phase<>'complete' OR outcome='complete')
);
CREATE INDEX ix_action_step_pending ON action_steps(operation,phase,target_id,action_id);
CREATE TABLE action_backups (
 action_id INTEGER NOT NULL REFERENCES actions(id),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 filename TEXT NOT NULL CHECK(length(filename)>0),
 retained_filename TEXT,
 digest TEXT CHECK(digest IS NULL OR (length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*')),
 bytes INTEGER CHECK(bytes IS NULL OR (typeof(bytes)='integer' AND bytes>0)),
 availability TEXT NOT NULL CHECK(availability IN ('verified','missing_historical')),
 PRIMARY KEY(action_id,position),
 UNIQUE(action_id,filename),
 CHECK((availability='verified' AND retained_filename IS NOT NULL AND digest IS NOT NULL AND bytes IS NOT NULL) OR
       (availability='missing_historical' AND retained_filename IS NULL AND digest IS NULL AND bytes IS NULL))
) WITHOUT ROWID;
CREATE TABLE action_target_claims (
 account_id INTEGER NOT NULL REFERENCES identities(id),
 playlist_scope_id TEXT NOT NULL REFERENCES playlist_scopes(ref),
 action_target_id INTEGER NOT NULL UNIQUE,
 claim_token TEXT NOT NULL CHECK(length(claim_token)=32),
 lease_until REAL NOT NULL CHECK(typeof(lease_until) IN ('integer','real') AND abs(lease_until)<=1.7976931348623157e308),
 PRIMARY KEY(account_id,playlist_scope_id),
 FOREIGN KEY(action_target_id,account_id,playlist_scope_id) REFERENCES action_targets(id,account_id,playlist_scope_id)
) WITHOUT ROWID;
CREATE TABLE action_plan_resolutions (
 action_id INTEGER NOT NULL REFERENCES actions(id),
 list_kind TEXT NOT NULL CHECK(list_kind IN ('additions','unresolved')),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 recording_id INTEGER REFERENCES recordings(id),
 unresolved_key TEXT,
 captured_identity_key TEXT NOT NULL,
 source_video_id_snapshot TEXT,
 target_video_id_snapshot TEXT,
 method TEXT NOT NULL CHECK(method IN ('reuse','search','unresolved')),
 PRIMARY KEY(action_id,list_kind,position),
 CHECK((recording_id IS NOT NULL AND unresolved_key IS NULL) OR
       (recording_id IS NULL AND unresolved_key IS NOT NULL AND unresolved_key=captured_identity_key))
) WITHOUT ROWID;
CREATE TABLE action_placement_intents (
 action_id INTEGER PRIMARY KEY REFERENCES actions(id),
 target_id INTEGER NOT NULL,
 after_video_id_snapshot TEXT,
 handles_present INTEGER NOT NULL CHECK(typeof(handles_present)='integer' AND handles_present IN (0,1)),
 FOREIGN KEY(target_id,action_id) REFERENCES action_targets(id,action_id)
);
CREATE TABLE action_placement_items (
 action_id INTEGER NOT NULL REFERENCES action_placement_intents(action_id),
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),
 PRIMARY KEY(action_id,position)
) WITHOUT ROWID;
CREATE TABLE action_placement_handles (
 action_id INTEGER NOT NULL REFERENCES action_placement_intents(action_id),
 video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),
 occurrence_handle_snapshot TEXT NOT NULL CHECK(length(occurrence_handle_snapshot)>0),
 PRIMARY KEY(action_id,video_id_snapshot)
) WITHOUT ROWID;
