"""Bounded idle conversion of impression-only policies and replay expiry."""
from .. import policy_history as owner
from ..db_writes import atomic_write


def prepare(store, *, now, limit=128):
    with atomic_write(store):
        return owner.prepare_batch(store.conn,now,limit)


def maintain(store, now):
    if not owner.available(store.conn):
        return 0
    converted = prepare(store,now=now)
    with atomic_write(store):
        expired = owner.expire(store.conn,now)
    with store._lock:
        owner.collect(store.conn,now)
    return converted+expired
