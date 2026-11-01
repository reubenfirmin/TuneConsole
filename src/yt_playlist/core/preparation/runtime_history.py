"""Explicit resumable runtime history preparation (#691 / #698).

python -m yt_playlist.core.preparation.runtime_history DATABASE credits --batches 10
python -m yt_playlist.core.preparation.runtime_history DATABASE modes --batches 10
"""
import argparse
import json
from pathlib import Path
import time

from .. import credit_retention, mode_retention
from ..db_writes import atomic_write

DOMAINS = {'credits': credit_retention, 'modes': mode_retention}


def step(store, domain, now=None, *, activate=False):
    with atomic_write(store):
        return DOMAINS[domain].step(store.conn, time.time() if now is None else now, activate=activate)


def status(conn, domain):
    table = 'artist_credit_retention' if domain == 'credits' else 'taste_mode_retention'
    return dict(conn.execute(f'SELECT * FROM {table} WHERE id=1').fetchone())


def maintain(store, now):
    """One bounded prefix per enabled owner, including idle worker turns."""
    return {domain: step(store, domain, now) for domain in DOMAINS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('domain', choices=DOMAINS)
    parser.add_argument('--batches', type=int, default=1)
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    from ..store import Store
    store = Store(args.database)
    try:
        store.init_schema()
        for _ in range(args.batches):
            if not step(store, args.domain, activate=True):
                break
        print(json.dumps(status(store.conn, args.domain)))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
