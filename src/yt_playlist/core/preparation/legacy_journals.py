"""Resumable cold archive receipt conversion; never a regular migration."""
from .. import legacy_journals as owner
from ..db_writes import atomic_write


def prepare(store, *, limit=128):
    count = 0
    for domain in owner.DOMAINS:
        with atomic_write(store):
            count += owner.prepare_batch(store.conn,domain,limit)
    return count


def maintain(store, now):
    if not owner.available(store.conn):
        return 0
    with atomic_write(store):
        count = owner.maintain(store.conn,now)
    with store._lock:
        return count + owner.collect(store.conn,now)
