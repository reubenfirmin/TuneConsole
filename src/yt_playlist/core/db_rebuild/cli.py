"""Connection and JSON framing for the explicit compaction command."""
import argparse
import json
from pathlib import Path


def main(argv=None, *, command, store_factory):
    parser = argparse.ArgumentParser(description='Construct and install a retained database copy.')
    parser.add_argument('database', type=Path)
    parser.add_argument('--scratch', type=Path, help='existing scratch directory; final custody stays beside the database')
    args = parser.parse_args(argv)
    if not args.database.is_file():
        parser.error('database must be an existing installed library')
    store = store_factory(args.database)
    try:
        store.init_schema(defer_metadata=True)
        report = command(store, scratch=args.scratch,
            progress=lambda phase, detail: print(json.dumps(dict(phase=phase, detail=detail)), flush=True))
        print(json.dumps(report, indent=2))
        return 0
    finally:
        store.conn.close()
