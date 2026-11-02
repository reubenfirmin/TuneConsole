"""Shared metadata admission rules, independent of evidence and file writers."""
import time

from .metadata_values import RECORDING_FIELDS, ITEM_FIELDS


ALTERNATIVE_SECONDS = 7 * 86400
PROVIDER_FIELDS = (*RECORDING_FIELDS, *ITEM_FIELDS, 'year', 'art', 'isrc', 'mb_release_ids', 'cover_url')


def clock(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Metadata evidence time must be finite')
    return now


def runtime_active(conn):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_runtime_state'").fetchone():
        return False
    return bool(conn.execute('SELECT active FROM metadata_runtime_state WHERE id=1').fetchone()[0])
