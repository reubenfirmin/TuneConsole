"""Publish complete clicked Home pools without blocking regular schema changes."""
import hashlib
from pathlib import Path

from ..schema_contract import MigrationError
from . import ranked_state as preparation
from .mapped_publication import MappedPublication
from .ranked_verification import source_row, verify_document


def native(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='clicked_library_pools'").fetchone() is not None


OWNER = MappedPublication('clicked', preparation.JOBS['clicked'], 'prepared_clicked_pools_generations',
    'prepared_clicked_pools_heads', native, verify_document, preparation.read_original,
    source_column='surface', generation_column='preparation_revision')


def repair(conn, surface):
    if conn.in_transaction:
        raise MigrationError('Ranked file recovery requires an idle connection')
    header, row = preparation.head(conn, surface), source_row(conn, surface)
    if header is None or row is None or surface not in preparation.library_pool_state.SURFACES:
        raise MigrationError('Clicked file recovery requires its retained source')
    raw = bytes(row['payload'])
    if hashlib.sha256(raw).hexdigest() != header['raw_digest'] or row['built_at'] != header['built_at']:
        raise MigrationError('Clicked source changed; reconcile before file recovery')
    plan = preparation.prepare(conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt clicked plan differs from its captured manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import json
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-surface', choices=preparation.library_pool_state.SURFACES)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=128)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                OWNER.publish(conn, stage=args.publish_stage)
                result = {'selected':OWNER.selected(conn)}
            elif args.repair_surface is not None:
                repair(conn, args.repair_surface)
                result = {'repaired':args.repair_surface}
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Clicked pool publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
