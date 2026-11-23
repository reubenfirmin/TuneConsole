"""Shared metadata admission rules, independent of evidence and file writers."""
import time
import json
from dataclasses import dataclass

from .db_revisions import setting_reads
from .metadata_values import RECORDING_FIELDS, ITEM_FIELDS


ACTIVE = 'metadata_runtime_active'
EXPIRY = 'metadata_expiry_checkpoint'
CAUGHT_UP = 'enrich_caught_up_at'
PREDECESSOR_ACTIVE_SQL = '(SELECT active FROM metadata_runtime_state WHERE id=1)'
ACTIVE_SQL = f"(SELECT CAST(value AS INTEGER) FROM settings WHERE key='{ACTIVE}')"
ALTERNATIVE_SECONDS = 7 * 86400
PROVIDER_FIELDS = (*RECORDING_FIELDS, *ITEM_FIELDS, 'year', 'art', 'isrc', 'mb_release_ids', 'cover_url', 'artist_credits')


@dataclass(frozen=True)
class Capture:
    entry: int
    recording: int
    video: str | None
    provider: str
    encoded: bytes
    run: str | None
    at: float

    @property
    def fields(self):
        return json.loads(self.encoded)


def clock(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Metadata evidence time must be finite')
    return now


def runtime_active(conn):
    with setting_reads(conn, (ACTIVE,)):
        active = conn.execute('SELECT value FROM settings WHERE key=?', (ACTIVE,)).fetchone()
    return active is not None and active[0] == '1'


def with_genre_keys(sql):
    return sql.replace('LEFT JOIN metadata_runtime_genres g ON g.id=c.genre_id', '').replace('g.genre_key', 'c.genre_key')
