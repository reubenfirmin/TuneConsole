"""Optional artwork recovery is independent of metadata pass completion."""
from .runner import MigrationError


ARTWORK_SQL = """CREATE TABLE enrichment_artwork (
    entry_id INTEGER PRIMARY KEY REFERENCES catalog_entries(id),
    release_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    failures INTEGER NOT NULL,
    retry_at REAL NOT NULL,
    error TEXT NOT NULL
)"""


def apply(conn, context):
    conn.execute(ARTWORK_SQL)


def validate(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='enrichment_artwork'").fetchone()
    if not row or row[0] != ARTWORK_SQL:
        raise MigrationError("Incompatible artwork retry table")
