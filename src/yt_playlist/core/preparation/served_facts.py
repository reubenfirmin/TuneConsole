"""Explicit restartable diagnostic extraction; never invoked during startup.

Run: python -m yt_playlist.core.preparation.served_facts DATABASE --batches 10
Repeat until active. Each batch commits independently; final activation also
checks for legacy writes admitted while earlier batches were running.
"""
import argparse
import json
from pathlib import Path

from .. import served_facts


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
        captured = 0
        for _ in range(args.batches):
            with store.conn:
                count = served_facts.prepare_batch(store.conn)
            captured += count
            if not count:
                break
        print(json.dumps({'captured': captured, 'active': served_facts.active(store.conn)}))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
