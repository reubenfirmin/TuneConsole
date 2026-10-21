"""Retain provider progress and retry deadlines across worker batches and restarts."""
from .runner import MigrationError


STATE_SQL = """CREATE TABLE enrichment_provider_state (
    recording_id INTEGER NOT NULL REFERENCES recordings(id),
    provider TEXT NOT NULL,
    cycle REAL NOT NULL,
    at REAL NOT NULL,
    status TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    failures INTEGER NOT NULL,
    retry_at REAL NOT NULL,
    error TEXT NOT NULL,
    PRIMARY KEY(recording_id,provider)
)"""
COOLDOWN_SQL = """CREATE TABLE enrichment_provider_cooldowns (
    provider TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    retry_at REAL NOT NULL,
    error TEXT NOT NULL
)"""


def apply(conn, context):
    conn.execute(STATE_SQL)
    conn.execute(COOLDOWN_SQL)


def validate(conn):
    for name, sql in (("enrichment_provider_state", STATE_SQL),
                      ("enrichment_provider_cooldowns", COOLDOWN_SQL)):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if not row or row[0] != sql:
            raise MigrationError(f"Incompatible enrichment retry table: {name}")
