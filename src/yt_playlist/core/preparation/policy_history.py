"""Bounded idle conversion of impression-only policies and replay expiry."""
from .. import policy_history as owner
from ..db_writes import atomic_write


def prepare(store, *, now, limit=128):
    # Searching a large policy inventory must not make Home's durable visit
    # wait for maintenance. A new owner can appear after this snapshot closes;
    # prepare_batch rechecks every selected policy under the writer reservation.
    with store.read_view() as reader:
        versions = owner.candidates(reader.conn, now, limit)
    if not versions:
        return 0
    with atomic_write(store):
        return owner.prepare_batch(store.conn,now,limit,versions=versions)


def maintain(store, now):
    with store._lock:
        if not owner.available(store.conn):
            return 0
    converted = prepare(store,now=now)
    with atomic_write(store):
        expired = owner.expire(store.conn,now)
    with store._lock:
        owner.collect(store.conn,now)
    return converted+expired
