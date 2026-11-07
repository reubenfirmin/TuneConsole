"""Run explicit preparation with python -m yt_playlist.core.preparation."""
import argparse
from contextlib import closing
import importlib
import json
from pathlib import Path
import time

from .. import fao
from ..store import Store
from .file_bodies import Conversion, PREPARATIONS, managed_roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--domain', choices=(*PREPARATIONS, 'compression'), required=True)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--logical-bytes', type=int, default=32*1024*1024,
                        help='Compression work budget per tick, at most 512 MiB')
    parser.add_argument('--assets', type=Path, action='append', default=[],
                        help='Additional managed asset tree, including packaged static/model resources')
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    if not 0 < args.logical_bytes <= fao.MAX_BYTES:
        parser.error('--logical-bytes must be between 1 and '+str(fao.MAX_BYTES))
    store = Store(args.database)
    try:
        store.init_schema()
        if args.domain == 'compression':
            roots = managed_roots(store.conn)
            roots.update((str(path), path) for path in args.assets)
            with closing(Conversion(roots)) as work:
                for _ in range(args.batches):
                    print(json.dumps(work.step(logical_bytes=args.logical_bytes)), flush=True)
                    if work.complete:
                        break
        else:
            owner = importlib.import_module(__package__+'.'+args.domain)
            for _ in range(args.batches):
                kwargs = {'now': time.time()} if args.domain == 'policy_history' else {}
                count = owner.prepare(store, **kwargs)
                print(json.dumps({'domain': args.domain, 'converted': count}), flush=True)
                if not count:
                    break
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
