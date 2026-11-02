"""Publish retained road-trip decisions without inventing authors or revisions."""
import hashlib

from .. import road_trip_draft_storage
from ..schema_contract import MigrationError
from . import road_trip_drafts as preparation
from .mapped_publication import MappedPublication


def source_row(conn, identity):
    return conn.execute("SELECT recipe_id,COALESCE(substr(CAST(state AS BLOB),1,?),X'') AS state,updated_at "
        'FROM main.road_trip_drafts WHERE recipe_id=?', (preparation.MAX_BYTES+1, identity)).fetchone()


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Draft verification requires an idle connection')
    row = source_row(conn, header['recipe_id'])
    if row is None:
        raise MigrationError('Retained draft source is missing')
    raw = bytes(row['state']).decode('utf-8')
    if preparation.read_original(conn, header) != raw:
        raise MigrationError('Prepared draft differs from its original')
    plan = preparation.describe(raw, row['updated_at'])
    expected = {'raw_digest':plan.digest, 'raw_size':plan.size, 'part_count':plan.part_count,
        'item_count':plan.item_count, 'updated_at':float(row['updated_at']),
        'source_revision':plan.source_revision, 'revision_kind':plan.revision_kind, 'building':int(plan.building),
        'account_id':None, 'legacy_author_unknown':1}
    if any(header[key] != value for key, value in expected.items()):
        raise MigrationError('Prepared draft fields differ from their original')
    try:
        conn.execute('BEGIN')
        readers = {kind:conn.execute('SELECT position,video_id,entry_id,recording_id,unresolved_reason FROM '+preparation.ITEMS+
            ' WHERE recipe_id=? AND generation=? AND selection_state=? ORDER BY position',
            (header['recipe_id'], header['generation'], kind)) for kind in ('picked', 'excluded')}
        for _, kind, position, video in plan.units:
            entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()
            expected = (position, video, entry[0] if entry else None, entry[1] if entry else None,
                None if entry else 'unadmitted_video')
            actual = readers[kind].fetchone()
            if actual is None or tuple(actual) != expected:
                raise MigrationError('Prepared draft selection differs from its original or catalog reference')
        if any(reader.fetchone() is not None for reader in readers.values()):
            raise MigrationError('Prepared draft contains extra selections')
    finally:
        conn.rollback()


OWNER = MappedPublication('drafts', preparation.JOB, preparation.HEAD, preparation.CURRENT,
    road_trip_draft_storage.available, verify_document, preparation.read_original,
    source_column='recipe_id', generation_column='generation')


def repair(conn, identity):
    if conn.in_transaction:
        raise MigrationError('Draft file recovery requires an idle connection')
    header = conn.execute('SELECT g.* FROM '+preparation.CURRENT+' h JOIN '+preparation.HEAD+
        ' g USING(recipe_id,generation) WHERE h.recipe_id=?', (identity,)).fetchone()
    row = source_row(conn, identity)
    if (header is None or row is None or hashlib.sha256(bytes(row['state'])).hexdigest() != header['raw_digest']
            or row['updated_at'] != header['updated_at']):
        raise MigrationError('Draft changed; reconcile before file recovery')
    retained = list(map(tuple, conn.execute('SELECT position,filename,digest,byte_size FROM '+preparation.PARTS+
        ' WHERE recipe_id=? AND generation=? ORDER BY position LIMIT ?',
        (identity, header['generation'], preparation.MAX_PARTS+1))))
    plan = preparation.prepare(conn, row)
    if retained != [(unit[1], *unit[2]) for unit in plan.units if unit[0] == 'file']:
        raise MigrationError('Rebuilt draft differs from its retained original manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import json
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-recipe', type=int)
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
                print(json.dumps({'selected':OWNER.selected(conn)}))
            elif args.repair_recipe is not None:
                repair(conn, args.repair_recipe)
                print(json.dumps({'repaired':args.repair_recipe}))
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                print(json.dumps(asdict(stage)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Draft publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
