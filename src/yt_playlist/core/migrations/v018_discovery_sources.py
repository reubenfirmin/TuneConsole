"""Give retained discovery evidence a type and a rediscovery clock."""
import sqlite3
from functools import lru_cache

from . import v008_recordings, v010_play_event_subjects


ALTERATIONS = (
    "ALTER TABLE discovery_sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE discovery_sources ADD COLUMN source_id TEXT",
    "ALTER TABLE discovery_sources ADD COLUMN last_found_at REAL",
)


def apply(conn, context):
    for sql in ALTERATIONS:
        conn.execute(sql)
    # Only retained tokens are evidence. No reconstruction of overwritten sources.
    conn.execute("UPDATE discovery_sources SET source_type=CASE "
                 "WHEN source_token LIKE 'radio:%' THEN 'radio' "
                 "WHEN source_token LIKE 'genre:%' THEN 'genre' ELSE 'album' END, "
                 "source_id=CASE WHEN source_token LIKE 'radio:%' OR source_token LIKE 'genre:%' "
                 "THEN substr(source_token,instr(source_token,':')+1) ELSE source_token END, "
                 "last_found_at=found_at")


@lru_cache(maxsize=1)
def expected_schema():
    with sqlite3.connect(':memory:') as reference:
        original = next(sql for name, _, sql in v010_play_event_subjects.expected_schema()
                        if name == 'discovery_sources')
        reference.execute(original)
        for sql in ALTERATIONS:
            reference.execute(sql)
        updated = reference.execute("SELECT sql FROM sqlite_master WHERE name='discovery_sources'").fetchone()[0]
    return [(name, kind, updated if name == 'discovery_sources' else sql)
            for name, kind, sql in v010_play_event_subjects.expected_schema()]


def validate(conn):
    v008_recordings.validate(conn, schema=expected_schema())
