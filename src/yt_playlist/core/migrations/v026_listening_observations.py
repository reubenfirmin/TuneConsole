"""Separate dated observations, event sources, and the account listening calendar.

Historical captures remain intact. Their timestamps are date/capture evidence of
unknown precision, not newly invented exact plays or known account timezones.
"""
from .runner import MigrationError


TABLES = (
    "CREATE TABLE listening_calendars (identity_id INTEGER PRIMARY KEY REFERENCES identities(id),timezone TEXT NOT NULL,confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)))",
    "CREATE TABLE listening_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,identity_id INTEGER NOT NULL REFERENCES identities(id),raw_identity_key TEXT NOT NULL,identity_key TEXT NOT NULL,recording_id INTEGER REFERENCES recordings(id),source_day TEXT NOT NULL CHECK(date(source_day) IS NOT NULL AND date(source_day)=source_day),day TEXT CHECK(day IS NULL OR (date(day) IS NOT NULL AND date(day)=day)),source TEXT NOT NULL,timezone TEXT NOT NULL,precision TEXT NOT NULL CHECK(precision='calendar_day'),first_observed_at REAL NOT NULL,last_observed_at REAL NOT NULL,UNIQUE(identity_id,raw_identity_key,source_day,source,timezone))",
    "CREATE TABLE play_event_sources (event_id INTEGER NOT NULL REFERENCES play_events(id) ON DELETE CASCADE,source TEXT NOT NULL,PRIMARY KEY(event_id,source))",
    "CREATE TABLE play_event_calendar (event_id INTEGER PRIMARY KEY REFERENCES play_events(id) ON DELETE CASCADE,day TEXT NOT NULL CHECK(date(day) IS NOT NULL AND date(day)=day),timezone TEXT NOT NULL)",
    "CREATE INDEX ix_listening_observation_subject_day ON listening_observations(identity_key,day,identity_id)",
    "CREATE INDEX ix_listening_observation_account_day ON listening_observations(identity_id,day)",
    "CREATE INDEX ix_play_event_source ON play_event_sources(source,event_id)",
    "CREATE INDEX ix_play_event_calendar_day ON play_event_calendar(day,event_id)",
)
PROJECTIONS = (
    """CREATE VIEW legacy_listening_observations AS
SELECT 'legacy:'||hi.rowid evidence_id,hs.identity_id,hi.identity_key,hi.recording_id,
       date(hs.taken_at,'unixepoch') day,hi.source,NULL timezone,
       'legacy_unknown' precision,hs.taken_at observed_at
FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id""",
    """CREATE VIEW listening_evidence AS
SELECT 'day:'||id evidence_id,identity_id,identity_key,recording_id,day,source,timezone,
       precision,last_observed_at observed_at,NULL event_id,0 is_generated
FROM listening_observations WHERE day IS NOT NULL
UNION ALL
SELECT evidence_id,identity_id,identity_key,recording_id,day,source,timezone,precision,
       observed_at,NULL,0 FROM legacy_listening_observations
UNION ALL
SELECT 'event:'||pe.id||':'||s.source,pe.identity_id,pe.identity_key,pe.recording_id,c.day,
       s.source,c.timezone,'exact_timestamp',pe.played_at,pe.id,COALESCE(pe.is_generated,0)
FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
JOIN play_event_sources s ON s.event_id=pe.id""",
    """CREATE VIEW eligible_listening_evidence AS
SELECT e.* FROM listening_evidence e
WHERE (e.event_id IS NOT NULL AND e.is_generated=0)
   OR (e.event_id IS NULL AND (e.source<>'ytm_history' OR NOT EXISTS (
       SELECT 1 FROM listening_evidence g WHERE g.event_id IS NOT NULL
       AND g.is_generated=1 AND g.identity_id=e.identity_id
       AND g.identity_key=e.identity_key AND g.day=e.day)
       OR EXISTS (SELECT 1 FROM listening_evidence organic
           WHERE organic.identity_id=e.identity_id AND organic.identity_key=e.identity_key
           AND organic.day=e.day AND organic.is_generated=0
           AND (organic.event_id IS NOT NULL OR organic.source<>'ytm_history'))))""",
    """CREATE VIEW listening_days AS
SELECT identity_id,identity_key,MIN(recording_id) recording_id,day,
       CAST(strftime('%s',day||' 12:00:00') AS REAL) day_ts,
       MAX(CASE WHEN event_id IS NOT NULL THEN observed_at END) last_event_at,
       MAX(observed_at) last_observed_at,
       CASE WHEN MIN(source)=MAX(source) THEN MIN(source) ELSE 'multiple' END source
FROM eligible_listening_evidence GROUP BY identity_id,identity_key,day""",
    """CREATE VIEW listening_daily_items AS
SELECT identity_id||':'||day||':'||identity_key rowid,identity_id||':'||day snapshot_id,
       identity_key,recording_id,source,day,identity_id,last_event_at,last_observed_at
FROM listening_days""",
    """CREATE VIEW listening_daily_snapshots AS
SELECT identity_id||':'||day id,identity_id,day_ts taken_at,day
FROM listening_days GROUP BY identity_id,day""",
    """CREATE VIEW listening_activity AS
SELECT pe.identity_id,pe.identity_key,pe.recording_id,c.day,pe.played_at at,
       'exact_timestamp' precision,pe.id event_id
FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
WHERE COALESCE(pe.is_generated,0)=0
UNION ALL
SELECT d.identity_id,d.identity_key,d.recording_id,d.day,d.day_ts,
       'calendar_day',NULL FROM listening_days d
WHERE NOT EXISTS (SELECT 1 FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
    WHERE pe.identity_id=d.identity_id AND pe.identity_key=d.identity_key AND c.day=d.day
    AND COALESCE(pe.is_generated,0)=0)""",
)
TRIGGERS = (
    """CREATE TRIGGER play_events_listening_insert AFTER INSERT ON play_events BEGIN
INSERT INTO play_event_sources(event_id,source) VALUES (NEW.id,COALESCE(NULLIF(NEW.source,''),'unknown'));
INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (NEW.id,date(NEW.played_at,'unixepoch'),'UTC');
END""",
)


def apply(conn,context):
    for sql in TABLES:
        conn.execute(sql)
    # UTC is an explicitly provisional projection, not a claim about the user's
    # location. The first valid browser calendar reprojects exact events.
    conn.execute("INSERT INTO listening_calendars SELECT id,'UTC',0 FROM identities")
    conn.execute("INSERT INTO play_event_sources SELECT id,COALESCE(NULLIF(source,''),'unknown') FROM play_events")
    conn.execute("INSERT INTO play_event_calendar SELECT id,date(played_at,'unixepoch'),'UTC' FROM play_events")
    for sql in (*PROJECTIONS,*TRIGGERS):
        conn.execute(sql)
    conn.execute('DELETE FROM trend_first_play')
    conn.execute("DELETE FROM settings WHERE key IN ('trend_first_play_watermark','trend_first_play_source')")


def validate(conn):
    for sql in (*TABLES,*PROJECTIONS,*TRIGGERS):
        parts = sql.split()
        name = parts[2]
        actual = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if not actual or actual[0]!=sql:
            raise MigrationError(f'Incompatible listening observation structure: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid listening observation reference')
    if conn.execute('SELECT 1 FROM play_events pe LEFT JOIN play_event_calendar c ON c.event_id=pe.id WHERE c.event_id IS NULL LIMIT 1').fetchone():
        raise MigrationError('Missing exact-event calendar projection')
    if conn.execute('SELECT 1 FROM play_events pe WHERE NOT EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id) LIMIT 1').fetchone():
        raise MigrationError('Missing exact-event source contribution')
