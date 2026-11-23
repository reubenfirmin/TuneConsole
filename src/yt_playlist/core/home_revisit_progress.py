"""One durable cursor for the independently completed Revisit summaries."""
from . import worker_settings

KEY = 'home_revisit_cursor'
FIELDS = ('rediscover', 'rediscover_albums', 'rediscover_tracks')


def read(conn):
    raw = worker_settings.read(conn, KEY)
    if raw is None:
        return None
    mask = int(raw)
    return [field for index, field in enumerate(FIELDS) if mask & (1 << index)]


def clear(conn):
    worker_settings.save(conn, KEY, None)


def save(conn, remaining):
    if not conn.in_transaction:
        raise ValueError('Home Revisit progress requires its summary publication')
    if any(field not in FIELDS for field in remaining):
        raise ValueError('Unknown Home Revisit component')
    mask = sum(1 << index for index, field in enumerate(FIELDS) if field in remaining)
    worker_settings.save(conn, KEY, str(mask))
