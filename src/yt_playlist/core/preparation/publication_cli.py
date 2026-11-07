"""Bounded CLI dispatch for staged publication owners."""
import argparse
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from ..db_handles import connect
from ..schema_contract import MigrationError


def main(owners, description, argv=None, *, label, repair, batch_size=128,
         repair_flag='--repair-source', repair_type=str, repair_choices=None):
    parser = argparse.ArgumentParser(description=description)
    named = isinstance(owners, dict)
    if named:
        parser.add_argument('job', choices=tuple(owners))
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument(repair_flag, dest='repair', type=repair_type, choices=repair_choices)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=batch_size)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    owner = owners[args.job] if named else owners
    prefix = {'job': args.job} if named else {}
    try:
        with closing(connect(args.database.resolve().as_uri() + '?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                owner.publish(conn, stage=args.publish_stage)
                result = {**prefix, 'selected': owner.selected(conn)}
            elif args.repair is not None:
                if named:
                    repair(args.job, conn, args.repair)
                else:
                    repair(conn, args.repair)
                result = {**prefix, 'repaired': args.repair}
            else:
                for _ in range(args.batches):
                    stage = owner.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'{label} publication failed: {exc}\n')
    return 0
