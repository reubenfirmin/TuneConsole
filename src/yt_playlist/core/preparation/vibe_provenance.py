"""Explicit bounded provenance conversion; never run by schema/startup (#699)."""
import argparse
from pathlib import Path
import time

from .. import vibe_provenance as owner, vibe_history
from ..db_writes import atomic_write


def prepare(store, *, limit=128):
    with atomic_write(store):
        return owner.prepare_batch(store.conn, limit)


def maintain(store, now):
    if not owner.available(store.conn):
        return 0
    with atomic_write(store):
        expired = owner.maintain(store.conn, now)
    retired = vibe_history.maintain(store, now)
    owner.collect(store.conn, now)
    return expired + retired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--batches', type=int, default=1)
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    from ..store import Store
    store = Store(args.database)
    try:
        store.init_schema()
        for _ in range(args.batches):
            if not prepare(store):
                break
        maintain(store, time.time())
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
