CREATE TABLE taste_settings_checkpoints (
 id INTEGER PRIMARY KEY CHECK(id=1),
 revision TEXT NOT NULL CHECK(length(revision)>0),
 saved_at REAL NOT NULL CHECK(typeof(saved_at) IN ('integer','real') AND abs(saved_at)<=1.7976931348623157e308),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
CREATE TABLE taste_checkpoint_weights (
 checkpoint_id INTEGER NOT NULL REFERENCES taste_settings_checkpoints(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 axis_id INTEGER NOT NULL REFERENCES taste_axes(id),
 captured_axis TEXT NOT NULL,
 weight REAL NOT NULL CHECK(typeof(weight) IN ('integer','real') AND abs(weight)<=1.7976931348623157e308),
 updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('integer','real') AND abs(updated_at)<=1.7976931348623157e308)),
 PRIMARY KEY(checkpoint_id,position),
 UNIQUE(checkpoint_id,axis_id)
) WITHOUT ROWID;
CREATE INDEX ix_taste_checkpoint_axis ON taste_checkpoint_weights(axis_id);
CREATE TABLE taste_comparison_baselines (
 id INTEGER PRIMARY KEY CHECK(id=1),
 revision TEXT NOT NULL CHECK(length(revision)>0),
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
CREATE TABLE taste_comparison_entries (
 baseline_id INTEGER NOT NULL REFERENCES taste_comparison_baselines(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
 recording_id INTEGER REFERENCES recordings(id),
 unresolved_key TEXT,
 captured_key TEXT NOT NULL CHECK(length(captured_key)>0),
 rank INTEGER CHECK(rank IS NULL OR (typeof(rank)='integer' AND rank>0)),
 score REAL NOT NULL CHECK(typeof(score) IN ('integer','real') AND abs(score)<=1.7976931348623157e308),
 lane TEXT NOT NULL,
 lane_weight REAL CHECK(lane_weight IS NULL OR (typeof(lane_weight) IN ('integer','real') AND abs(lane_weight)<=1.7976931348623157e308)),
 PRIMARY KEY(baseline_id,position),
 CHECK((recording_id IS NOT NULL AND unresolved_key IS NULL) OR (recording_id IS NULL AND unresolved_key IS NOT NULL AND length(unresolved_key)>0))
) WITHOUT ROWID;
CREATE UNIQUE INDEX ix_comparison_recording ON taste_comparison_entries(baseline_id,recording_id) WHERE recording_id IS NOT NULL;
CREATE UNIQUE INDEX ix_comparison_unresolved ON taste_comparison_entries(baseline_id,unresolved_key) WHERE unresolved_key IS NOT NULL;
CREATE TABLE experiment_decisions (
 id INTEGER PRIMARY KEY,
 decision TEXT NOT NULL CHECK(decision IN ('promote','retire')),
 cohort TEXT NOT NULL CHECK(length(cohort)<=100),
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
 created_at REAL NOT NULL CHECK(typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308),
 powered INTEGER NOT NULL CHECK(typeof(powered)='integer' AND powered IN (0,1)),
 basis TEXT NOT NULL,
 recovery_filename TEXT NOT NULL,
 recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
 recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
CREATE INDEX ix_experiment_decision_cohort ON experiment_decisions(cohort,created_at);
