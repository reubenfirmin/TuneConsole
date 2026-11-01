CREATE TABLE album_menu_controls (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 expires_at REAL NOT NULL CHECK(typeof(expires_at) IN ('integer','real') AND abs(expires_at)<=1.7976931348623157e308),
 novelty REAL NOT NULL CHECK(typeof(novelty) IN ('integer','real') AND novelty BETWEEN 0 AND 1)
);
CREATE TABLE album_menu_control_genres (
 singleton INTEGER NOT NULL REFERENCES album_menu_controls(singleton) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 99),
 genre_key TEXT REFERENCES genre_catalog(key),
 unresolved_value TEXT CHECK(unresolved_value IS NULL OR length(unresolved_value) BETWEEN 1 AND 100),
 PRIMARY KEY(singleton,position),
 CHECK((genre_key IS NOT NULL AND unresolved_value IS NULL) OR (genre_key IS NULL AND unresolved_value IS NOT NULL))
) WITHOUT ROWID;
CREATE INDEX ix_album_menu_control_genre ON album_menu_control_genres(genre_key);
CREATE TABLE model_selection_heads (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 active_run_id INTEGER REFERENCES rec_model_selection_runs(id),
 display_run_id INTEGER REFERENCES rec_model_selection_runs(id),
 live_run_id INTEGER REFERENCES rec_model_selection_runs(id),
 restore_run_id INTEGER REFERENCES rec_model_selection_runs(id)
);
CREATE TABLE candidate_refresh_checkpoint (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 preferences_fingerprint TEXT,
 expires_at REAL CHECK(expires_at IS NULL OR (typeof(expires_at) IN ('integer','real') AND abs(expires_at)<=1.7976931348623157e308))
);
CREATE TABLE vibe_preparation_checkpoint (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 policy_version TEXT
);
CREATE TABLE taste_model_state (
 id TEXT PRIMARY KEY NOT NULL CHECK(id IN ('recording','artist')),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 generation TEXT,
 input_fingerprint TEXT,
 genre_revision TEXT,
 fit_fingerprint TEXT,
 recovery_filename TEXT,
 recovery_digest TEXT CHECK(recovery_digest IS NULL OR length(recovery_digest)=64),
 recovery_bytes INTEGER CHECK(recovery_bytes IS NULL OR (typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)),
 CHECK((recovery_filename IS NULL AND recovery_digest IS NULL AND recovery_bytes IS NULL) OR (recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL AND recovery_bytes IS NOT NULL))
) WITHOUT ROWID;
CREATE TABLE content_basis_checkpoint (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 serial INTEGER CHECK(serial IS NULL OR (typeof(serial)='integer' AND serial>=0)),
 rows INTEGER CHECK(rows IS NULL OR (typeof(rows)='integer' AND rows>=0)),
 at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
 reason TEXT,
 estimated_bytes INTEGER CHECK(estimated_bytes IS NULL OR (typeof(estimated_bytes)='integer' AND estimated_bytes>=0)),
 dimensions INTEGER CHECK(dimensions IS NULL OR (typeof(dimensions)='integer' AND dimensions>=0)),
 input_fingerprint TEXT,
 has_modes INTEGER NOT NULL CHECK(has_modes IN (0,1)),
 mode_count INTEGER CHECK(mode_count IS NULL OR (typeof(mode_count)='integer' AND mode_count>0)),
 mode_min_members INTEGER CHECK(mode_min_members IS NULL OR (typeof(mode_min_members)='integer' AND mode_min_members>0)),
 mode_match_threshold REAL CHECK(mode_match_threshold IS NULL OR (typeof(mode_match_threshold) IN ('integer','real') AND mode_match_threshold BETWEEN 0 AND 1)),
 has_phases INTEGER NOT NULL CHECK(has_phases IN (0,1)),
 bytes_encoding INTEGER CHECK(bytes_encoding IS NULL OR (typeof(bytes_encoding)='integer' AND bytes_encoding>=0)),
 bytes_clustering INTEGER CHECK(bytes_clustering IS NULL OR (typeof(bytes_clustering)='integer' AND bytes_clustering>=0)),
 bytes_publication INTEGER CHECK(bytes_publication IS NULL OR (typeof(bytes_publication)='integer' AND bytes_publication>=0)),
 CHECK((has_modes=0 AND mode_count IS NULL AND mode_min_members IS NULL AND mode_match_threshold IS NULL)
    OR (has_modes=1 AND mode_count IS NOT NULL AND mode_min_members IS NOT NULL AND mode_match_threshold IS NOT NULL)),
 CHECK(has_phases=1 OR (bytes_encoding IS NULL AND bytes_clustering IS NULL AND bytes_publication IS NULL))
);
CREATE TABLE content_full_checkpoint (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 content INTEGER CHECK(content IS NULL OR (typeof(content)='integer' AND content>=0)),
 evidence INTEGER CHECK(evidence IS NULL OR (typeof(evidence)='integer' AND evidence>=0)),
 rows INTEGER CHECK(rows IS NULL OR (typeof(rows)='integer' AND rows>=0)),
 at REAL CHECK(at IS NULL OR (typeof(at) IN ('integer','real') AND abs(at)<=1.7976931348623157e308)),
 has_config INTEGER NOT NULL CHECK(has_config IN (0,1)),
 embed_method TEXT,
 dimensions_setting TEXT,
 cooc_weighting TEXT,
 restore_run TEXT,
 mode_count_setting TEXT,
 mode_min_members_setting TEXT,
 mode_match_threshold_setting TEXT
);
CREATE TABLE content_fit_checkpoint (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 state TEXT CHECK(state IN ('ready','waiting','deferred')),
 reason TEXT,
 attempted_at REAL CHECK(attempted_at IS NULL OR (typeof(attempted_at) IN ('integer','real') AND abs(attempted_at)<=1.7976931348623157e308)),
 attempts INTEGER CHECK(attempts IS NULL OR (typeof(attempts)='integer' AND attempts>=0)),
 since REAL CHECK(since IS NULL OR (typeof(since) IN ('integer','real') AND abs(since)<=1.7976931348623157e308)),
 next_attempt_at REAL CHECK(next_attempt_at IS NULL OR (typeof(next_attempt_at) IN ('integer','real') AND abs(next_attempt_at)<=1.7976931348623157e308)),
 memory_bytes INTEGER CHECK(memory_bytes IS NULL OR (typeof(memory_bytes)='integer' AND memory_bytes>=0)),
 estimated_bytes INTEGER CHECK(estimated_bytes IS NULL OR (typeof(estimated_bytes)='integer' AND estimated_bytes>=0)),
 rows INTEGER CHECK(rows IS NULL OR (typeof(rows)='integer' AND rows>=0)),
 dimensions INTEGER CHECK(dimensions IS NULL OR (typeof(dimensions)='integer' AND dimensions>=0)),
 max_rows INTEGER CHECK(max_rows IS NULL OR (typeof(max_rows)='integer' AND max_rows>=0)),
 has_phases INTEGER NOT NULL CHECK(has_phases IN (0,1)),
 bytes_encoding INTEGER CHECK(bytes_encoding IS NULL OR (typeof(bytes_encoding)='integer' AND bytes_encoding>=0)),
 bytes_clustering INTEGER CHECK(bytes_clustering IS NULL OR (typeof(bytes_clustering)='integer' AND bytes_clustering>=0)),
 bytes_publication INTEGER CHECK(bytes_publication IS NULL OR (typeof(bytes_publication)='integer' AND bytes_publication>=0)),
 CHECK(has_phases=1 OR (bytes_encoding IS NULL AND bytes_clustering IS NULL AND bytes_publication IS NULL))
);
CREATE TABLE group_preparation_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 fit_fingerprint TEXT,
 phase TEXT CHECK(phase IN ('preparing','ready','deferred')),
 reported_at REAL CHECK(reported_at IS NULL OR (typeof(reported_at) IN ('integer','real') AND abs(reported_at)<=1.7976931348623157e308)),
 reason TEXT,
 generation_id INTEGER REFERENCES group_generations(id),
 unresolved_generation TEXT,
 input_serial INTEGER CHECK(input_serial IS NULL OR (typeof(input_serial)='integer' AND input_serial>=0)),
 checkpoint_at REAL CHECK(checkpoint_at IS NULL OR (typeof(checkpoint_at) IN ('integer','real') AND abs(checkpoint_at)<=1.7976931348623157e308)),
 CHECK((phase IS NULL AND reported_at IS NULL AND reason IS NULL) OR (phase IS NOT NULL AND reported_at IS NOT NULL)),
 CHECK((generation_id IS NULL AND unresolved_generation IS NULL AND input_serial IS NULL AND checkpoint_at IS NULL)
  OR (((generation_id IS NOT NULL AND unresolved_generation IS NULL) OR (generation_id IS NULL AND unresolved_generation IS NOT NULL)) AND input_serial IS NOT NULL AND checkpoint_at IS NOT NULL))
);
