"""Explicit bounded float32 conversion; legacy and file heads stay readable."""
from .. import embedding_files as owner
from ..db_writes import atomic_write


def prepare(store, *, limit=128):
    count = 0
    for kind in ('recording', 'artist'):
        with atomic_write(store):
            count += owner.prepare_batch(store.conn, kind, limit)
    return count


def maintain(store, now):
    if not owner.available(store.conn):
        return 0
    with store._lock:
        return owner.collect(store.conn, now)
