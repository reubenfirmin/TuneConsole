"""Frozen physical schema adopted by migration 1. Do not edit after release."""

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  brand_account_id TEXT,
  is_master INTEGER NOT NULL DEFAULT 0,
  last_auth_ok REAL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(label)
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  video_id TEXT,
  title TEXT, artist TEXT, album TEXT, duration_s INTEGER,
  orig_title TEXT, orig_artist TEXT,
  identity_key TEXT NOT NULL,
  available INTEGER,
  video_type TEXT,
  artist_browse_id TEXT,
  album_browse_id TEXT,
  thumbnail TEXT,
  genre TEXT,
  mb_year TEXT,
  bpm REAL,
  energy REAL,
  danceability REAL,
  mb_recording_id TEXT,
  music_key TEXT,
  music_scale TEXT,
  mood_happy REAL,
  mood_sad REAL,
  mood_relaxed REAL,
  mood_acoustic REAL,
  instrumental REAL,
  loudness REAL,
  dynamic_complexity REAL,
  popularity INTEGER,
  gain REAL,
  label TEXT,
  UNIQUE(identity_key, video_id)
);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  ytm_playlist_id TEXT NOT NULL,
  title TEXT, track_count INTEGER,
  content_hash TEXT,
  first_seen REAL, last_seen REAL, last_changed REAL,
  created_at REAL,                        -- actual creation time; NULL when unknown, never a sync date
  created_at_source TEXT,                 -- local creation or authoritative youtube metadata
  thumbnail TEXT,
  UNIQUE(identity_id, ytm_playlist_id)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  position INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tracks_null_vid ON tracks(identity_key) WHERE video_id IS NULL;
CREATE TABLE IF NOT EXISTS history_snapshots (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  taken_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history_items (
  snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id),
  identity_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS play_events (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  identity_key TEXT NOT NULL,              -- #75 live now-playing stream: one row per real play,
  video_id TEXT,                           -- real timestamps (the (track,day) history model stays
  played_at REAL NOT NULL,                 -- the coarse view every existing consumer reads)
  playlist_ytm_id TEXT,                    -- provenance: the list= id when played from a playlist
  like_status TEXT,                        -- LIKE | DISLIKE | INDIFFERENT at report time
  is_generated INTEGER CHECK(is_generated IN (0,1)), -- play-time policy; NULL = unknown historical provenance
  -- Where this row came from. 'takeout' is account-wide (every device) and is therefore AUTHORITATIVE
  -- for the span it covers: a history row it does not corroborate never happened. 'live' comes from
  -- the browser extension and sees only browser plays, so its silence proves nothing. '' is legacy.
  source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_play_events_time ON play_events(played_at);
-- #93v2 recent_plays_with_ts' radio-shadow suppression probes play_events by identity_key per
-- history row; without this index that is a correlated full scan (measured ~25 s at 33k history
-- rows x 20k events, vs 67 ms indexed). Also serves import_play_events' (identity, key, ts) dedup.
CREATE INDEX IF NOT EXISTS ix_play_events_key ON play_events(identity_key);
CREATE TABLE IF NOT EXISTS player_events (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  kind TEXT NOT NULL,                      -- #91 raw sensor stream: track_exit | ended | state |
  video_id TEXT,                           -- tick | volume | bye | rate | playlist_edit |
  position REAL,                           -- feedback | subscription | share_intent
  duration REAL,
  playlist_ytm_id TEXT,
  payload TEXT,                            -- JSON extras per kind (state/volume/shuffle/repeat or url/body/href/action)
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_player_events_at ON player_events(at);
CREATE INDEX IF NOT EXISTS ix_player_events_kind_at ON player_events(kind, at);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  params_json TEXT, plan_json TEXT, undo_json TEXT,
  status TEXT NOT NULL,
  created_at REAL, executed_at REAL
);
CREATE TABLE IF NOT EXISTS suppressed_overlaps (
  a TEXT NOT NULL, b TEXT NOT NULL, created_at REAL,
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS overlap_ignored (
  ytm TEXT PRIMARY KEY, created_at REAL
);
CREATE TABLE IF NOT EXISTS overlap_kept (
  a TEXT NOT NULL, b TEXT NOT NULL, created_at REAL,
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS cleanup_ignored (
  ytm TEXT NOT NULL, category TEXT NOT NULL,   -- category: 'empty' | 'tiny' (per-playlist, scoped)
  created_at REAL, PRIMARY KEY (ytm, category)
);
CREATE TABLE IF NOT EXISTS ignored_merges (
  signature TEXT PRIMARY KEY,   -- canonical sorted member ytm ids joined ("A|B|C"): one merge suggestion
  members TEXT NOT NULL,        -- JSON list of member ytm ids (for display / reconstruction)
  created_at REAL
);
CREATE TABLE IF NOT EXISTS playlist_group (
  ytm TEXT PRIMARY KEY, name TEXT NOT NULL   -- user-assigned group name for a playlist
);
CREATE TABLE IF NOT EXISTS hidden_playlists (
  ytm TEXT PRIMARY KEY   -- playlists hidden from the Playlists tab (e.g. undeletable system ones)
);
CREATE TABLE IF NOT EXISTS saved_albums (
  browse_id TEXT PRIMARY KEY, title TEXT, artist TEXT, year TEXT, type TEXT, thumbnail TEXT
);
-- Full endpoint responses, separate from incidental album tracks found in playlists.
CREATE TABLE IF NOT EXISTS album_tracklists (
  browse_id TEXT PRIMARY KEY, title TEXT, artist TEXT, thumbnail TEXT,
  track_keys TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT   -- small app settings, e.g. lastfm_api_key
);
CREATE TABLE IF NOT EXISTS genre_whitelist (
  name TEXT PRIMARY KEY COLLATE NOCASE   -- editable genre whitelist for tag matching
);
-- Identity, equivalent labels and family membership are separate from the enabled tag list.
CREATE TABLE IF NOT EXISTS genre_family (
  key TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS genre_catalog (
  key TEXT PRIMARY KEY, name TEXT NOT NULL,
  family_key TEXT REFERENCES genre_family(key),
  source TEXT NOT NULL, family_source TEXT, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS genre_alias (
  label_key TEXT PRIMARY KEY, label TEXT NOT NULL,
  genre_key TEXT NOT NULL REFERENCES genre_catalog(key), source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_genre_alias_genre ON genre_alias(genre_key);
CREATE TABLE IF NOT EXISTS genre_external_id (
  provider TEXT NOT NULL, external_id TEXT NOT NULL,
  genre_key TEXT NOT NULL REFERENCES genre_catalog(key),
  PRIMARY KEY (provider, external_id)
);
CREATE TABLE IF NOT EXISTS enrichment_log (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL,
  run_id TEXT NOT NULL,                   -- groups one waterfall run across providers/tracks
  provider TEXT NOT NULL,                 -- 'musicbrainz' | 'lastfm' | ...
  field TEXT NOT NULL,                    -- neutral concept: 'genre' | 'year' | 'bpm' | ...
  value TEXT,                             -- provider's finding, stringified (NULL = found nothing)
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_enrichment_log_track ON enrichment_log(track_id);
CREATE TABLE IF NOT EXISTS enrichment_conflict (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL,
  field TEXT NOT NULL,                    -- conflicting field: 'genre' | 'year' | 'bpm'
  candidates TEXT NOT NULL,               -- JSON: [{"provider": ..., "value": ...}, ...]
  resolved INTEGER NOT NULL DEFAULT 0,
  resolved_value TEXT,
  updated_at REAL,
  UNIQUE(track_id, field)
);
CREATE TABLE IF NOT EXISTS rec_vectors (
  identity_key TEXT PRIMARY KEY,
  vec BLOB NOT NULL                       -- float32 taste-embedding for the track (see embed.py)
);
CREATE TABLE IF NOT EXISTS rec_model_selection_runs (
  id INTEGER PRIMARY KEY,
  started_at REAL NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rec_model_previous_vectors (
  run_id INTEGER NOT NULL REFERENCES rec_model_selection_runs(id),
  identity_key TEXT NOT NULL,
  vec BLOB NOT NULL,
  PRIMARY KEY (run_id, identity_key)
);
CREATE TABLE IF NOT EXISTS rec_model_forecasts (
  run_id INTEGER NOT NULL REFERENCES rec_model_selection_runs(id),
  made_at REAL NOT NULL,
  forecast_json TEXT NOT NULL,
  PRIMARY KEY (run_id, made_at)
);
CREATE TABLE IF NOT EXISTS rec_content_vectors (
  identity_key TEXT PRIMARY KEY,
  vec BLOB NOT NULL                       -- float32 content (genre/era) vector; see embed.build_content_vectors
);
CREATE TABLE IF NOT EXISTS rec_discovered_content_vectors (
  identity_key TEXT PRIMARY KEY,
  vec BLOB NOT NULL                       -- #13 P2: out-of-corpus track content vectors (same model space)
);
CREATE TABLE IF NOT EXISTS rec_artist_vectors (
  artist TEXT PRIMARY KEY,                -- normalized artist name (util.matching.normalize)
  vec BLOB NOT NULL                       -- #28 float32 collaborative artist embedding (see rec/artist_model.py)
);
CREATE TABLE IF NOT EXISTS rec_artist_content_vectors (
  artist TEXT PRIMARY KEY,                -- normalized artist name
  vec BLOB NOT NULL                       -- #28 float32 artist content (genre/era/audio) vector, same model space
);
CREATE TABLE IF NOT EXISTS rec_feedback (
  surface TEXT NOT NULL,                  -- where it happened: 'for_you', 'suggest', 'discover'
  item_key TEXT NOT NULL,                 -- track identity_key (or 'artist:<name>' for a mute)
  kind TEXT NOT NULL,                     -- 'dismiss' | 'less' | 'more' | 'mute' | 'not_now'
  reason TEXT,                            -- optional axis/reason ('era','artist','vibe','own_it')
  scope TEXT NOT NULL DEFAULT '',         -- '' = global; else a playlist id for playlist-local
  until REAL,                             -- suppressed until ts (NULL = until explicitly cleared)
  created_at REAL,
  PRIMARY KEY (surface, item_key, scope)
);
CREATE TABLE IF NOT EXISTS rec_weights (
  axis TEXT PRIMARY KEY,                  -- e.g. 'lane:deep_cut', 'family:techno'
  weight REAL NOT NULL DEFAULT 1.0,       -- learned blend weight; 1.0 = prior
  updated_at REAL                         -- #85 last nudge time; drives time-proportional reversion at read
);
CREATE TABLE IF NOT EXISTS rec_lean (
  axis TEXT PRIMARY KEY,                  -- 'genre:<fam>' | 'genre:<sub>' | 'era:<decade>' | 'artist:<name>'
  value REAL NOT NULL DEFAULT 1.0,        -- standing transient multiplier; 1.0 = neutral (non-decaying)
  updated_at REAL,                        -- when the slider was last moved (drives held-day exposure)
  last_graduated_day TEXT                 -- UTC date (YYYY-MM-DD) of last exposure-graduation, or NULL
);
-- Home steering bars the user has hidden ("remove this bar"). Pure DISPLAY curation: which steering
-- bars show on Home. Does NOT change recommendations (that's leans/weights): a hidden axis just
-- isn't offered as a bar. Re-adding it via the genre picker un-hides it; "Reset to default" clears all.
CREATE TABLE IF NOT EXISTS home_hidden_facet (
  axis TEXT PRIMARY KEY                   -- 'genre:<name>' | 'era:<decade>' hidden from the Home panel
);
CREATE TABLE IF NOT EXISTS wiki_cards (
  subject    TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  display    TEXT NOT NULL,
  title      TEXT,
  extract    TEXT,
  thumbnail  TEXT,
  url        TEXT,
  found      INTEGER NOT NULL,
  fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS taste_modes (
  mode_id     INTEGER PRIMARY KEY,
  label       TEXT NOT NULL,
  families    TEXT NOT NULL,
  centroid    BLOB NOT NULL,
  size        INTEGER NOT NULL,
  rep_keys    TEXT NOT NULL,
  active      INTEGER NOT NULL,
  first_seen  REAL NOT NULL,
  last_seen   REAL NOT NULL,
  -- Fingerprint of the content space `centroid` lives in (embed.content_model_fingerprint). A
  -- centroid is only comparable to one from the same space; '' means "built before we tracked
  -- this", which never matches and so retires cleanly on the next recompute.
  space       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rec_mode_impressions (
  epoch      INTEGER NOT NULL,
  lane       TEXT NOT NULL,
  mode_id    INTEGER NOT NULL,
  ranker     TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (epoch, lane)
);
CREATE TABLE IF NOT EXISTS rec_mode_picks (
  playlist_id INTEGER PRIMARY KEY,
  mode_id     INTEGER NOT NULL,
  ranker      TEXT,
  created_at  REAL NOT NULL
);
-- #87 raw log of which lane served each rendered Home item: the future lane-bandit's training data
CREATE TABLE IF NOT EXISTS rec_lane_impressions (
  id INTEGER PRIMARY KEY,
  lane TEXT NOT NULL,                      -- #87 which home lane served the item
  identity_key TEXT NOT NULL,              -- what was shown (future lane-bandit success join)
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lane_impressions_at ON rec_lane_impressions(at);
-- #76-#79 first-play index: earliest UTC day we have evidence of a track / artist. Built by the rec
-- worker (trend_rollups) as MIN over the day model and play_events; play_events only lowers the MIN.
CREATE TABLE IF NOT EXISTS trend_first_play (
  kind      TEXT NOT NULL,        -- 'track' (identity_key) | 'artist' (display name)
  id_key    TEXT NOT NULL,
  first_day INTEGER NOT NULL,     -- int(first_ts // 86400)
  first_ts  REAL NOT NULL,
  source    TEXT NOT NULL,        -- 'history' | 'play_event'
  PRIMARY KEY (kind, id_key)
);


CREATE TABLE IF NOT EXISTS artist_scans (
  artist TEXT PRIMARY KEY,
  scanned_at REAL                         -- last time we hit YT/Last.fm for this artist
);
CREATE TABLE IF NOT EXISTS discovered_albums (
  browse_id TEXT PRIMARY KEY,
  artist TEXT, title TEXT, year TEXT, thumbnail TEXT,
  found_at REAL,                          -- first time we discovered it (drives recency)
  last_shown REAL,                        -- last time it was surfaced on Home (anti-repeat)
  genre TEXT                              -- #18: candidate genre for the facet overlay
);
CREATE TABLE IF NOT EXISTS discovered_artists (
  artist TEXT PRIMARY KEY,
  score REAL, because TEXT, fits TEXT, thumbnail TEXT,
  found_at REAL, last_shown REAL,
  genre TEXT                              -- #18: candidate genre for the facet overlay
);
CREATE TABLE IF NOT EXISTS discovered_tracks (
  identity_key TEXT PRIMARY KEY,          -- #13 Phase 2: out-of-corpus candidate tracks for clusters
  video_id TEXT, title TEXT, artist TEXT, album TEXT, thumbnail TEXT,
  genre TEXT, year TEXT,
  bpm REAL, energy REAL, danceability REAL, mood_happy REAL, mood_sad REAL,
  mood_relaxed REAL, mood_acoustic REAL, instrumental REAL, loudness REAL,
  dynamic_complexity REAL, music_key TEXT, music_scale TEXT,
  source_browse_id TEXT,                  -- the discovered album/artist it came from
  found_at REAL,
  last_enriched REAL                      -- #50: when the cold-enrichment waterfall last probed it (NULL = queued)
);
CREATE TABLE IF NOT EXISTS discovery_visible_impressions (
  view_id TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, shown_at REAL NOT NULL,
  PRIMARY KEY (view_id, kind, item_id)
);


-- per-(surface, item) view counts + last_shown: anti-staleness erosion and per-card rotation
CREATE TABLE IF NOT EXISTS rec_impressions (
  surface TEXT NOT NULL,                  -- 'for_you' | 'explore' | 'suggest'
  item_key TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,       -- times shown (debounced), for anti-staleness erosion
  last_shown REAL,                        -- drives the recycle cooldown
  PRIMARY KEY (surface, item_key)
);
-- materialized last-good proposal payloads per surface (rec worker writes, routes read)
CREATE TABLE IF NOT EXISTS rec_proposals (
  surface TEXT PRIMARY KEY,               -- 'fresh_songs' | 'discover'
  payload TEXT NOT NULL,                  -- JSON, materialized by the rec worker (last-good serving)
  built_at REAL
);
-- Last.fm similar-artist cache (14-day TTL); also the #28 artist model's edge source
CREATE TABLE IF NOT EXISTS rec_artist_similar (
  artist TEXT PRIMARY KEY,                -- anchor artist (display name)
  payload TEXT NOT NULL,                  -- JSON [[name, match], ...] from Last.fm
  fetched_at REAL
);
-- count-capped log of mood feedback events (the transient model's mood seed)
CREATE TABLE IF NOT EXISTS rec_mood (
  created_at REAL NOT NULL,               -- when the mood feedback was given (drives time-decay)
  direction INTEGER NOT NULL,             -- +1 = more of this vibe, -1 = not my mood
  keys TEXT NOT NULL                      -- JSON list of the playlist's track identity_keys (the seed)
);
-- the theme/params a generated playlist was built from (legible + re-runnable)
CREATE TABLE IF NOT EXISTS rec_recipes (
  playlist_ytm TEXT PRIMARY KEY,          -- the generated playlist's YouTube id
  recipe TEXT NOT NULL,                   -- JSON: the rolled theme + params + dj seed + version
  created_at REAL
);
-- the full canvas blob behind a saved cluster playlist, so it can be reopened and regrown (#48)
CREATE TABLE IF NOT EXISTS cluster_canvas (
  playlist_ytm TEXT PRIMARY KEY,          -- the generated playlist's YouTube id (#48: reopen its cluster)
  state TEXT NOT NULL,                     -- JSON: the full canvas blob (nodes, trunk, seeds, filter, view)
  created_at REAL
);
-- graduation ledger: persistent signed running totals (the transient -> permanent funnel)
CREATE TABLE IF NOT EXISTS rec_theme (
  facet      TEXT PRIMARY KEY,             -- 'genre:<fam>' | 'era:<decade>' | 'artist:<name>'
  score      REAL NOT NULL,                -- persistent signed running total (interaction-driven, no time decay)
  updated_at REAL NOT NULL                 -- bookkeeping only
);
-- per-axis once-per-UTC-day stamp: the play-exposure graduation idempotency guard
CREATE TABLE IF NOT EXISTS rec_play_grad (
  axis               TEXT PRIMARY KEY,      -- facet token, e.g. 'genre:techno'
  last_graduated_day TEXT                   -- UTC YYYY-MM-DD the play-exposure funnel last graduated this axis
);
-- append-only graduation instrumentation: one row per threshold-crossing nudge (model-health panel)
CREATE TABLE IF NOT EXISTS rec_grad_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  axis       TEXT NOT NULL,                 -- the facet that graduated, e.g. 'genre:techno'
  source     TEXT NOT NULL,                 -- the signal that drove it: 'play' | 'slider' | 'vibe' | 'like' | ...
  score      REAL NOT NULL,                 -- the ledger value at the crossing (what tripped THEME_THRESHOLD)
  factor     REAL NOT NULL,                 -- the nudge multiplier applied (graduate_up / graduate_down)
  new_weight REAL NOT NULL,                 -- the resulting permanent weight after the nudge
  created_at REAL NOT NULL
);
-- #113 what the recommender actually put on screen. Outcomes are deliberately derived from the
-- immutable play/player ledgers at read time, so attribution rules can improve without rewriting
-- history. One request/rank is one opportunity; retries of the same render are idempotent.
CREATE TABLE IF NOT EXISTS rec_served_impressions (
  id                INTEGER PRIMARY KEY,
  request_id        TEXT NOT NULL,
  surface           TEXT NOT NULL,
  lane              TEXT NOT NULL,
  mode_id           INTEGER,
  identity_key      TEXT NOT NULL,
  rank               INTEGER NOT NULL,
  score              REAL,
  model_version      TEXT NOT NULL,
  provenance         TEXT NOT NULL,
  served_at          REAL NOT NULL,
  UNIQUE(request_id, surface, rank)
);
CREATE INDEX IF NOT EXISTS ix_rec_served_at ON rec_served_impressions(served_at);
CREATE INDEX IF NOT EXISTS ix_rec_served_key_at ON rec_served_impressions(identity_key, served_at);


CREATE TABLE IF NOT EXISTS road_trip_recipes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL,
  own_pct          INTEGER NOT NULL,
  artists          TEXT NOT NULL,
  genres           TEXT NOT NULL,
  blacklist_genres TEXT NOT NULL,   -- legacy: the genre bars on the draft replaced it
  target_minutes   INTEGER NOT NULL,
  last_playlist_id TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS road_trip_drafts (
  recipe_id  INTEGER PRIMARY KEY,
  state      TEXT NOT NULL,
  updated_at REAL NOT NULL
);

"""

