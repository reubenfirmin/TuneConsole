"""Explicit bounded profile conversion, independent of startup and migrations."""
import argparse
from pathlib import Path
import time

from .. import profile_files as owner
from ..db_writes import atomic_write


def prepare(store, *, limit=128):
    count = 0
    for kind in owner.KINDS:
        with atomic_write(store):
            count += owner.prepare_batch(store.conn, kind, limit)
    return count


def maintain(store, now):
    if not owner.available(store.conn):
        return 0
    with store._lock:
        return owner.collect(store.conn, now)


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
