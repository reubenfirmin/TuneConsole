"""Verify and select ordered recipe membership while retaining compatible writers."""
from dataclasses import asdict
import hashlib
import json

from .. import recipe_memberships
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from . import recipe_memberships as preparation
from .mapped_publication import MappedPublication


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Recipe verification requires an idle connection')
    source = preparation._source(conn, header['recipe_id'])
    if source is None:
        raise MigrationError('Retained recipe source is missing')
    encoded, lists = preparation._documents(source)
    original = preparation.read_original(header)
    if (original != json.loads(encoded) or header['digest'] != hashlib.sha256(encoded).hexdigest()
            or header['byte_size'] != len(encoded) or header['members_count'] != sum(map(len, lists))):
        raise MigrationError('Prepared recipe original differs from its installed source')
    try:
        conn.execute('BEGIN')
        reader = conn.execute('SELECT role,position,raw_label,artist_id,genre_key,unresolved_reason,duplicate_position '
            'FROM '+preparation.ITEMS+' WHERE recipe_id=? AND generation=? ORDER BY role,position',
            (header['recipe_id'], header['generation']))
        for role, values in sorted(zip(preparation.ROLES, lists)):
            seen = {}
            for position, label in enumerate(values):
                member = preparation.describe(conn, role, position, label)
                identity = member[3] if role == 'artist' else member[4]
                duplicate = seen.get(identity) if identity is not None else None
                expected = (*member, duplicate)
                actual = reader.fetchone()
                if actual is None or tuple(actual) != expected:
                    raise MigrationError('Prepared recipe membership differs from its source or identity references')
                if duplicate is not None:
                    raise MigrationError('Recipe selects the same identity twice; repair the source before publication')
                if identity is not None:
                    seen[identity] = position
        if reader.fetchone() is not None:
            raise MigrationError('Prepared recipe contains extra membership')
    finally:
        conn.rollback()


OWNER = MappedPublication('recipe-memberships', preparation.JOB, preparation.HEAD, 'prepared_recipe_heads',
    recipe_memberships.available, verify_document, preparation.read_original,
    source_column='recipe_id', generation_column='generation')


def read(conn, recipe_id):
    stage = OWNER.selected(conn)
    if stage is None:
        return None
    rows = conn.execute('SELECT r.role,r.position,CASE WHEN r.role=\'artist\' THEN '
        'COALESCE(a.name,r.raw_label) ELSE COALESCE(c.name,r.raw_label) END AS label '
        'FROM '+OWNER.table('map')+' m JOIN '+preparation.ITEMS+
        ' r ON r.recipe_id=m.source AND r.generation=m.generation '
        'LEFT JOIN artists a ON a.id=r.artist_id LEFT JOIN genre_catalog c ON c.key=r.genre_key '
        'WHERE m.stage=? AND m.source=? ORDER BY r.role,r.position', (stage, recipe_id))
    fields = dict(zip(preparation.ROLES, preparation.FIELDS))
    result = {field:[] for field in preparation.FIELDS}
    for role, _, label in rows:
        result[fields[role]].append(label)
    return result


def repair(conn, recipe_id):
    if conn.in_transaction:
        raise MigrationError('Recipe file recovery requires an idle connection')
    header = conn.execute('SELECT g.* FROM prepared_recipe_heads h JOIN '+preparation.HEAD+
        ' g USING(recipe_id,generation) WHERE recipe_id=?', (recipe_id,)).fetchone()
    source = preparation._source(conn, recipe_id)
    if header is None or source is None:
        raise MigrationError('Recipe original is missing; reconcile before recovery')
    encoded, _ = preparation._documents(source)
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != header['digest'] or len(encoded) != header['byte_size']:
        raise MigrationError('Recipe changed; reconcile before file recovery')
    filename = _publish_encoded(preparation.directory(conn), encoded, digest)
    if str(filename) != header['filename']:
        raise MigrationError('Rebuilt recipe has a different original path')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-recipe', type=int)
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=16)
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
            elif args.repair_recipe is not None:
                repair(conn, args.repair_recipe)
                result = {'repaired':args.repair_recipe}
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                result = asdict(stage)
            print(json.dumps(result))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Recipe publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
