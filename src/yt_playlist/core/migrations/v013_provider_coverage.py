"""Provider configuration and targeted coverage work, independent of recording timestamps."""
from .runner import MigrationError


CONFIG_SQL = """CREATE TABLE enrichment_provider_config (
    provider TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL
)"""
JOBS_SQL = """CREATE TABLE enrichment_provider_jobs (
    recording_id INTEGER NOT NULL REFERENCES recordings(id),
    provider TEXT NOT NULL,
    PRIMARY KEY(recording_id,provider)
)"""


def apply(conn, context):
    conn.execute(CONFIG_SQL)
    conn.execute(JOBS_SQL)


def validate(conn):
    for name, sql in (("enrichment_provider_config", CONFIG_SQL),
                      ("enrichment_provider_jobs", JOBS_SQL)):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if not row or row[0] != sql:
            raise MigrationError(f"Incompatible provider coverage table: {name}")
