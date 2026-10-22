"""Retain historical captures as explicit observations of unknown play precision.

Capture receipts preserve even empty batches. Their timestamps describe when the
history window was captured; neither those times nor their UTC date projections
establish an exact play or a confirmed account timezone.
"""
from . import v026_listening_observations as previous
from .runner import MigrationError


CAPTURES = (
    "CREATE TABLE listening_capture_receipts (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),captured_at REAL NOT NULL)",
    "CREATE INDEX ix_listening_capture_account_time ON listening_capture_receipts(identity_id,captured_at)",
)
OBSERVATIONS = """CREATE TABLE listening_observations (
id INTEGER PRIMARY KEY AUTOINCREMENT,
identity_id INTEGER NOT NULL REFERENCES identities(id),
raw_identity_key TEXT,
identity_key TEXT NOT NULL,
recording_id INTEGER REFERENCES recordings(id),
source_day TEXT CHECK(source_day IS NULL OR (date(source_day) IS NOT NULL AND date(source_day)=source_day)),
day TEXT CHECK(day IS NULL OR (date(day) IS NOT NULL AND date(day)=day)),
source TEXT NOT NULL,
timezone TEXT,
precision TEXT NOT NULL,
first_observed_at REAL NOT NULL,
last_observed_at REAL NOT NULL,
capture_id INTEGER REFERENCES listening_capture_receipts(id),
capture_item_id INTEGER UNIQUE,
CHECK((precision='calendar_day' AND timezone IS NOT NULL AND raw_identity_key IS NOT NULL
       AND source_day IS NOT NULL AND capture_id IS NULL AND capture_item_id IS NULL)
   OR (precision='legacy_unknown' AND timezone IS NULL
       AND capture_id IS NOT NULL AND capture_item_id IS NOT NULL)),
UNIQUE(identity_id,raw_identity_key,source_day,source,timezone))"""
INDEXES = (
    *previous.TABLES[4:6],
    "CREATE INDEX ix_listening_observation_recording_day ON listening_observations(recording_id,day,identity_id)",
    "CREATE INDEX ix_listening_observation_capture ON listening_observations(capture_id)",
)
EVIDENCE = """CREATE VIEW listening_evidence AS
SELECT CASE WHEN capture_item_id IS NOT NULL THEN 'capture:'||capture_item_id ELSE 'day:'||id END evidence_id,
       identity_id,identity_key,recording_id,day,source,timezone,
       precision,last_observed_at observed_at,NULL event_id,0 is_generated
FROM listening_observations WHERE day IS NOT NULL
UNION ALL
SELECT 'event:'||pe.id||':'||s.source,pe.identity_id,pe.identity_key,pe.recording_id,c.day,
       s.source,c.timezone,'exact_timestamp',pe.played_at,pe.id,COALESCE(pe.is_generated,0)
FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
JOIN play_event_sources s ON s.event_id=pe.id"""
ACTIVITY = """CREATE VIEW listening_activity AS
SELECT pe.identity_id,pe.identity_key,pe.recording_id,c.day,pe.played_at at,
       'exact_timestamp' precision,pe.id event_id
FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
WHERE COALESCE(pe.is_generated,0)=0
UNION ALL
SELECT d.identity_id,d.identity_key,d.recording_id,d.day,d.day_ts,
       CASE WHEN EXISTS (SELECT 1 FROM eligible_listening_evidence e
           WHERE e.identity_id=d.identity_id AND e.identity_key=d.identity_key AND e.day=d.day
           AND e.precision='calendar_day') THEN 'calendar_day' ELSE 'legacy_unknown' END,
       NULL FROM listening_days d
WHERE NOT EXISTS (SELECT 1 FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
    WHERE pe.identity_id=d.identity_id AND pe.identity_key=d.identity_key AND c.day=d.day
    AND COALESCE(pe.is_generated,0)=0)"""
PROJECTIONS = (EVIDENCE, *previous.PROJECTIONS[2:6], ACTIVITY)
OLD_VIEWS = (
    'listening_activity', 'listening_daily_snapshots', 'listening_daily_items',
    'listening_days', 'eligible_listening_evidence', 'listening_evidence',
    'legacy_listening_observations',
)
RETIRED = {
    'history_items', 'history_snapshots', 'legacy_listening_observations',
    'history_items_subject', 'ix_history_item_key_snapshot',
    'ix_history_item_recording_snapshot', 'ix_history_snapshot_account_time',
}


def apply(conn, context):
    for name in OLD_VIEWS:
        conn.execute(f'DROP VIEW {name}')
    for sql in previous.TABLES[4:6]:
        conn.execute(f'DROP INDEX {sql.split()[2]}')
    conn.execute('ALTER TABLE listening_observations RENAME TO capture_conversion_observations')
    for sql in (*CAPTURES, OBSERVATIONS, *INDEXES):
        conn.execute(sql)
    conn.execute('INSERT INTO listening_capture_receipts SELECT id,identity_id,taken_at FROM history_snapshots')
    fields = ('id,identity_id,raw_identity_key,identity_key,recording_id,source_day,day,'
              'source,timezone,precision,first_observed_at,last_observed_at')
    conn.execute(f'INSERT INTO listening_observations({fields}) '
                 f'SELECT {fields} FROM capture_conversion_observations')
    conn.execute(
        'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,recording_id,'
        'source_day,day,source,timezone,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
        "SELECT hs.identity_id,hi.raw_identity_key,hi.identity_key,hi.recording_id,date(hs.taken_at,'unixepoch'),"
        "date(hs.taken_at,'unixepoch'),hi.source,NULL,'legacy_unknown',hs.taken_at,hs.taken_at,hs.id,hi.rowid "
        'FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id ORDER BY hi.rowid')
    # Verify all original rows and empty receipts before retiring their storage.
    missing = conn.execute(
        'SELECT hs.identity_id,hi.raw_identity_key,hi.identity_key,hi.recording_id,hi.source,'
        'hs.taken_at,hs.id,hi.rowid FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id '
        'EXCEPT SELECT identity_id,raw_identity_key,identity_key,recording_id,source,'
        'last_observed_at,capture_id,capture_item_id FROM listening_observations '
        "WHERE precision='legacy_unknown' LIMIT 1").fetchone()
    if missing or conn.execute('SELECT COUNT(*) FROM history_items').fetchone()[0] != conn.execute(
            "SELECT COUNT(*) FROM listening_observations WHERE precision='legacy_unknown'").fetchone()[0]:
        raise MigrationError('Historical capture conversion lost original evidence')
    if conn.execute('SELECT id,identity_id,taken_at FROM history_snapshots EXCEPT '
                    'SELECT id,identity_id,captured_at FROM listening_capture_receipts LIMIT 1').fetchone():
        raise MigrationError('Historical capture conversion lost a receipt')
    conn.execute('DROP TABLE capture_conversion_observations')
    conn.execute('DROP TABLE history_items')
    conn.execute('DROP TABLE history_snapshots')
    for sql in PROJECTIONS:
        conn.execute(sql)
    conn.execute('DELETE FROM trend_first_play')
    conn.execute("DELETE FROM settings WHERE key IN ('trend_first_play_watermark','trend_first_play_source')")


def validate(conn):
    for sql in (*CAPTURES, OBSERVATIONS, *INDEXES, *PROJECTIONS):
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] != sql:
            raise MigrationError(f'Incompatible capture evidence structure: {name}')
    for name in RETIRED:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
            raise MigrationError(f'Retired capture structure remains: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid capture evidence reference')
    if conn.execute('SELECT 1 FROM listening_observations o JOIN listening_capture_receipts c '
                    'ON c.id=o.capture_id WHERE o.identity_id<>c.identity_id '
                    'OR o.first_observed_at IS NOT c.captured_at OR o.last_observed_at IS NOT c.captured_at '
                    'LIMIT 1').fetchone():
        raise MigrationError('Historical capture receipt does not match its observation')
