"""Select exact Home files together with their retained evidence custody."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .. import home_outputs as outputs, recommendation_outputs as files
from ..schema_contract import MigrationError
from . import home_outputs as preparation
from .pair_publication import RetainedPublication


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='home_display_publications'").fetchone() is not None


def source(conn, surface):
    return conn.execute('SELECT '+','.join(preparation.DEFINITION.projections['rec_proposals'])+
        ' FROM main.rec_proposals WHERE surface=?', (surface,)).fetchone()


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Home verification requires an idle connection')
    row = source(conn, header['surface'])
    if row is None or preparation.read_original(conn, header) != bytes(row['payload']).decode('utf-8'):
        raise MigrationError('Home original differs from its installed source')
    value = outputs.prepare(row['surface'], bytes(row['payload']).decode('utf-8'), row['built_at'])
    policies = sorted(outputs.policy_references(value.value))
    fields = (value.surface, header['generation'], value.built_at, value.schema, value.generation, value.mix_regions,
        value.digest, len(value.raw.encode()), len(value.parts), len(policies), int(value.raw not in ('null', '{}')))
    if tuple(header) != fields:
        raise MigrationError('Home output metadata differs from its installed original')
    parts = conn.execute('SELECT position,filename,digest,byte_size FROM '+preparation.PARTS+
        ' WHERE surface=? AND generation=? ORDER BY position', (header['surface'], header['generation'])).fetchall()
    if len(parts) != len(value.parts):
        raise MigrationError('Home display has missing or extra parts')
    for position, (part, encoded) in enumerate(zip(parts, value.parts)):
        if (part['position'], part['digest'], part['byte_size']) != (position, hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise MigrationError('Home part differs from its installed original')
        if files._read(part['filename'], part['digest'], part['byte_size'], surface=header['surface'], chunk_key=str(position)) != encoded:
            raise MigrationError('Home file bytes differ from their installed original')
    expected = []
    for key in policies:
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (key,)).fetchone()
        expected.append((key, key if known else None, None if known else 'missing_policy'))
    actual = conn.execute('SELECT raw_key,policy_version,unresolved_reason FROM '+preparation.POLICIES+
        ' WHERE surface=? AND generation=? ORDER BY raw_key', (header['surface'], header['generation'])).fetchall()
    if [tuple(row) for row in actual] != expected:
        raise MigrationError('Home policy references differ from their installed original')


class HomePublication(RetainedPublication):
    roots = preparation
    root_schema = preparation
    root_kind = 'prepared'
    root_namespace = 'home-output'
    root_tag = 'home'
    title = 'Home'

    @staticmethod
    def serialize_root(value):
        # Existing Home archives preserve projection order; pair archives sort it.
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


OWNER = HomePublication('home-files', preparation.JOB, preparation.HEAD, preparation.CURRENT,
    native, verify_document, preparation.read_original, source_column='surface', generation_column='generation')


def header(conn, surface):
    """Capture one selection and its immutable header in the caller's snapshot."""
    if surface not in outputs.SURFACES:
        return False, None
    owned = not conn.in_transaction
    try:
        if owned:
            conn.execute('BEGIN')
        identity = OWNER.selected(conn)
        return (False, None) if identity is None else (True, OWNER.capture(conn, identity, surface))
    finally:
        if owned:
            conn.rollback()


def read(conn, surface):
    handled, captured = header(conn, surface)
    if not handled:
        return False, None
    try:
        return True, preparation.read_original(conn, captured) if captured is not None else None
    except (OSError, ValueError, MigrationError):
        return True, None


def repair(conn, surface):
    if conn.in_transaction:
        raise MigrationError('Home file recovery requires an idle connection')
    captured = conn.execute('SELECT g.* FROM '+preparation.CURRENT+' h JOIN '+preparation.HEAD+
        ' g USING(surface,generation) WHERE surface=?', (surface,)).fetchone()
    row = source(conn, surface)
    if (captured is None or row is None or hashlib.sha256(bytes(row['payload'])).hexdigest() != captured['raw_digest']
            or row['built_at'] != captured['built_at']):
        raise MigrationError('Home source changed or is missing; reconcile before recovery')
    plan = preparation.prepare(conn, row)
    retained = conn.execute('SELECT filename,digest,byte_size FROM '+preparation.PARTS+
        ' WHERE surface=? AND generation=? ORDER BY position', (surface, captured['generation'])).fetchall()
    if [tuple(row) for row in retained] != [unit[1] for unit in plan.units if unit[0] == 'file']:
        raise MigrationError('Recovered Home files differ from their retained manifests')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-surface', choices=sorted(outputs.SURFACES))
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=8)
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
                result = {'selected': OWNER.selected(conn)}
            elif args.repair_surface is not None:
                repair(conn, args.repair_surface)
                result = {'repaired': args.repair_surface}
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Home publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
