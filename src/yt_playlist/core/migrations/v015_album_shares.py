"""Ordered shareable album snapshots require stronger evidence than cached membership."""
from .runner import MigrationError


ALBUM_SHARES_SQL = """CREATE TABLE album_share_snapshots (
    browse_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    video_ids_json TEXT NOT NULL
)"""


def apply(conn, context):
    # Old denominators contain sorted, deduplicated recording keys, so their original item
    # order cannot be reconstructed safely. Populate share snapshots only from future full reads.
    conn.execute(ALBUM_SHARES_SQL)


def validate(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='album_share_snapshots'").fetchone()
    if not row or row[0] != ALBUM_SHARES_SQL:
        raise MigrationError("Incompatible album share snapshot table")
