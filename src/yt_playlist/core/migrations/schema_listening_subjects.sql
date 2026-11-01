CREATE TABLE play_events (
 id INTEGER PRIMARY KEY,
 identity_id INTEGER NOT NULL REFERENCES identities(id),
 raw_identity_key TEXT,
 recording_id INTEGER REFERENCES recordings(id),
 unresolved_subject_key TEXT,
 video_id TEXT, played_at REAL NOT NULL, playlist_ytm_id TEXT,
 membership_ref_id INTEGER REFERENCES playlist_membership_refs(id), like_status TEXT,
 is_generated INTEGER CHECK(is_generated IN (0,1)),
 CHECK((recording_id IS NOT NULL AND unresolved_subject_key IS NULL)
    OR (recording_id IS NULL AND unresolved_subject_key IS NOT NULL))
);
CREATE TABLE listening_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 identity_id INTEGER NOT NULL REFERENCES identities(id),
 raw_identity_key TEXT,
 recording_id INTEGER REFERENCES recordings(id),
 unresolved_subject_key TEXT,
 source_day TEXT CHECK(source_day IS NULL OR (date(source_day) IS NOT NULL AND date(source_day)=source_day)),
 day TEXT CHECK(day IS NULL OR (date(day) IS NOT NULL AND date(day)=day)),
 source TEXT NOT NULL, timezone TEXT, precision TEXT NOT NULL,
 first_observed_at REAL NOT NULL, last_observed_at REAL NOT NULL,
 capture_id INTEGER REFERENCES listening_capture_receipts(id), capture_item_id INTEGER UNIQUE,
 CHECK((recording_id IS NOT NULL AND unresolved_subject_key IS NULL)
    OR (recording_id IS NULL AND unresolved_subject_key IS NOT NULL)),
 CHECK((precision='calendar_day' AND timezone IS NOT NULL AND raw_identity_key IS NOT NULL
        AND source_day IS NOT NULL AND capture_id IS NULL AND capture_item_id IS NULL)
    OR (precision='legacy_unknown' AND timezone IS NULL AND capture_id IS NOT NULL AND capture_item_id IS NOT NULL)),
 UNIQUE(identity_id,raw_identity_key,source_day,source,timezone)
);
CREATE VIEW play_event_subjects AS
 SELECT p.*,coalesce(r.identity_key,p.unresolved_subject_key) identity_key
 FROM play_events p LEFT JOIN recordings r ON r.id=p.recording_id;
CREATE VIEW listening_observation_subjects AS
 SELECT o.*,coalesce(r.identity_key,o.unresolved_subject_key) identity_key
 FROM listening_observations o LEFT JOIN recordings r ON r.id=o.recording_id;
CREATE INDEX ix_play_event_account_subject_time ON play_events(identity_id,recording_id,played_at);
CREATE INDEX ix_play_event_unresolved_time ON play_events(unresolved_subject_key,played_at) WHERE recording_id IS NULL;
CREATE INDEX ix_play_event_unresolved_raw ON play_events(raw_identity_key) WHERE recording_id IS NULL;
CREATE INDEX ix_listening_observation_unresolved_day ON listening_observations(unresolved_subject_key,day,identity_id) WHERE recording_id IS NULL;
CREATE INDEX ix_listening_observation_unresolved_raw ON listening_observations(raw_identity_key) WHERE recording_id IS NULL;
CREATE INDEX ix_listening_observation_canonical_scope ON listening_observations(identity_id,recording_id,source_day,timezone) WHERE precision='calendar_day';
CREATE TABLE recording_listening_summary (
 account_id INTEGER NOT NULL REFERENCES identities(id), recording_id INTEGER NOT NULL REFERENCES recordings(id),
 first_exact_play_at REAL, first_exact_day TEXT, first_observed_day TEXT, first_legacy_unknown_day TEXT,
 build_revision INTEGER NOT NULL,
 CHECK((first_exact_play_at IS NULL)=(first_exact_day IS NULL)),
 PRIMARY KEY(account_id,recording_id)
) WITHOUT ROWID;
CREATE TABLE artist_listening_summary (
 account_id INTEGER NOT NULL REFERENCES identities(id), artist_id INTEGER NOT NULL REFERENCES artists(id),
 first_exact_play_at REAL, first_exact_day TEXT, first_observed_day TEXT, first_legacy_unknown_day TEXT,
 build_revision INTEGER NOT NULL,
 CHECK((first_exact_play_at IS NULL)=(first_exact_day IS NULL)),
 PRIMARY KEY(account_id,artist_id)
) WITHOUT ROWID;
CREATE TABLE listening_summary_state (
 id INTEGER PRIMARY KEY CHECK(id=1), input_revision INTEGER NOT NULL DEFAULT 0,
 built_revision INTEGER, built_at REAL
);
CREATE VIEW recording_listening_artists AS
 SELECT DISTINCT e.recording_id,coalesce(r.artist_id,c.artist_id) artist_id
 FROM catalog_entries e JOIN entry_artists c ON c.entry_id=e.id
 LEFT JOIN artist_redirects r ON r.retired_id=c.artist_id;
CREATE VIEW trend_first_play AS
 SELECT 'track' kind,r.identity_key id_key,
 CAST(julianday(min(s.day))-julianday('1970-01-01') AS INTEGER) first_day,
 min(s.exact_at) first_ts,'canonical_listening' source
 FROM (SELECT recording_id,first_exact_day day,first_exact_play_at exact_at FROM recording_listening_summary
       UNION ALL SELECT recording_id,first_observed_day,NULL FROM recording_listening_summary
       UNION ALL SELECT recording_id,first_legacy_unknown_day,NULL FROM recording_listening_summary) s
 JOIN recordings r ON r.id=s.recording_id WHERE s.day IS NOT NULL GROUP BY s.recording_id
 UNION ALL
 SELECT 'artist','@artist:'||s.artist_id,CAST(julianday(min(s.day))-julianday('1970-01-01') AS INTEGER),
 min(s.exact_at),'canonical_listening'
 FROM (SELECT artist_id,first_exact_day day,first_exact_play_at exact_at FROM artist_listening_summary
       UNION ALL SELECT artist_id,first_observed_day,NULL FROM artist_listening_summary
       UNION ALL SELECT artist_id,first_legacy_unknown_day,NULL FROM artist_listening_summary) s
 WHERE s.day IS NOT NULL GROUP BY s.artist_id
 UNION ALL
 SELECT 'track',identity_key,CAST(julianday(min(day))-julianday('1970-01-01') AS INTEGER),
 min(CASE WHEN precision='exact_timestamp' THEN at END),'unresolved_listening'
 FROM listening_activity WHERE recording_id IS NULL AND day IS NOT NULL GROUP BY identity_key;
