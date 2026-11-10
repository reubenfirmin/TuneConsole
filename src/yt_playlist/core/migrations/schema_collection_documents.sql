CREATE TABLE spotify_import_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision TEXT NOT NULL CHECK(length(revision)=32),
  account_id INTEGER REFERENCES identities(id),
  unresolved_account_id INTEGER,
  account_present INTEGER NOT NULL CHECK(account_present IN (0,1)),
  field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 4095),
  status TEXT NOT NULL CHECK(status IN ('queued','running','preview','done','error')),
  phase TEXT CHECK(phase IN ('preview','import')),
  started_at REAL CHECK(started_at IS NULL OR (typeof(started_at) IN ('integer','real') AND abs(started_at)<=1.7976931348623157e308)),
  finished_at REAL CHECK(finished_at IS NULL OR (typeof(finished_at) IN ('integer','real') AND abs(finished_at)<=1.7976931348623157e308)),
  identity_label TEXT,
  report_schema INTEGER CHECK(report_schema IS NULL OR (typeof(report_schema)='integer' AND report_schema>=0)),
  dismissed INTEGER CHECK(dismissed IS NULL OR dismissed IN (0,1)),
  error TEXT,
  playlists_found INTEGER CHECK(playlists_found IS NULL OR (typeof(playlists_found)='integer' AND playlists_found>=0)),
  track_entries INTEGER CHECK(track_entries IS NULL OR (typeof(track_entries)='integer' AND track_entries>=0)),
  albums_found INTEGER CHECK(albums_found IS NULL OR (typeof(albums_found)='integer' AND albums_found>=0)),
  lookup_count INTEGER CHECK(lookup_count IS NULL OR (typeof(lookup_count)='integer' AND lookup_count>=0)),
  estimate_label TEXT,
  recovery_filename TEXT NOT NULL,
  recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
  recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216),
  CHECK((account_id IS NULL OR unresolved_account_id IS NULL) AND
        (account_present=1 OR (account_id IS NULL AND unresolved_account_id IS NULL)))
);
CREATE TABLE spotify_import_job_head (
  id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
  job_id INTEGER NOT NULL UNIQUE REFERENCES spotify_import_jobs(id)
);
CREATE INDEX ix_spotify_import_job_account ON spotify_import_jobs(account_id,status,id);
CREATE TABLE rec_recipes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_scope_id TEXT NOT NULL UNIQUE REFERENCES playlist_scopes(ref),
  revision TEXT NOT NULL CHECK(length(revision)=32),
  created_at REAL CHECK(created_at IS NULL OR (typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308)),
  media TEXT CHECK(media IS NULL OR media IN ('audio','video')),
  model TEXT,
  journey TEXT,
  opportunity_id TEXT,
  option_id TEXT,
  recovery_filename TEXT NOT NULL,
  recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
  recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
CREATE TABLE playlist_recipe_facets (
  recipe_id INTEGER NOT NULL REFERENCES rec_recipes(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('genre','era')),
  position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),
  axis_id INTEGER NOT NULL REFERENCES taste_axes(id),
  captured_value TEXT NOT NULL CHECK(length(captured_value)>0),
  PRIMARY KEY(recipe_id,kind,position)
) WITHOUT ROWID;
CREATE INDEX ix_playlist_recipe_axis ON playlist_recipe_facets(axis_id,recipe_id);
CREATE TABLE cluster_canvas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_scope_id TEXT NOT NULL UNIQUE REFERENCES playlist_scopes(ref),
  revision TEXT NOT NULL CHECK(length(revision)=32),
  created_at REAL CHECK(created_at IS NULL OR (typeof(created_at) IN ('integer','real') AND abs(created_at)<=1.7976931348623157e308)),
  recovery_filename TEXT NOT NULL,
  recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64 AND recovery_digest NOT GLOB '*[^0-9a-f]*'),
  recovery_bytes INTEGER NOT NULL CHECK(typeof(recovery_bytes)='integer' AND recovery_bytes BETWEEN 1 AND 16777216)
);
