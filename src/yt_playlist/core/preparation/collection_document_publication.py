"""Publish verified saved recipes and canvases through retained playlist maps."""
from functools import partial
import hashlib

from .. import collection_recovery, playlist_recipe_state
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import collection_documents as preparation
from .document_publication import verify_paged_plan
from .mapped_publication import MappedPublication


def source_row(conn, name, source):
    owner, key, field = preparation.SOURCES[name]
    return conn.execute('SELECT '+key+' AS source,CASE WHEN '+field+' IS NULL THEN NULL ELSE '
        'COALESCE(substr(CAST('+field+" AS BLOB),1,?),X'') END AS document,created_at FROM main."+
        owner+' WHERE '+key+'=?', (preparation.MAX_SOURCE_BYTES+1, source)).fetchone()


def verify_document(name, conn, header):
    if conn.in_transaction:
        raise MigrationError('Saved document verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(conn, name, source)
    if row is None:
        raise MigrationError('Saved document source is missing')
    raw = bytes(row['document']).decode('utf-8') if row['document'] is not None else None
    if preparation.read_original(conn, name, header) != raw:
        raise MigrationError('Saved document differs from its installed original')
    fields, value = preparation.describe(name, raw, row['created_at'])
    known = conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (source,)).fetchone()
    fields.update(scope_ref=known[0] if known else None, scope_resolution=None if known else 'missing_scope')
    parts = conn.execute('SELECT position,filename,content_digest,byte_size FROM '+preparation.table(name, 'parts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
        (source, revision, fields['part_count']+1)).fetchall()
    if len(parts) != fields['part_count']:
        raise MigrationError('Saved document original parts are incomplete')
    units = [('part', tuple(part)) for part in parts]
    if name == 'recipes':
        units.extend(preparation.recipe_units(conn, value, fields))
    if fields['state_present']:
        kind = 'playlist-recipe' if name == 'recipes' else 'playlist-canvas'
        reference = tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
        encoded = canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':source, 'state':value})
        if reference[1:] != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise MigrationError('Saved document recovery differs from its original')
        collection_recovery.read(*reference, kind=kind, owner=source)
        fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), reference))
    fields['unit_count'] = len(units)
    verify_paged_plan(source, header, fields, units)
    if any(header[key] != (float(value) if key == 'created_at' and value is not None else value)
           for key, value in fields.items()):
        raise MigrationError('Saved document fields differ from their original or references')
    # Captured rows are immutable already. A read snapshot keeps this ordered
    # comparison coherent without reserving the writer or reading files in SQL.
    try:
        conn.execute('BEGIN')
        if name == 'recipes':
            readers = {kind:conn.execute('SELECT * FROM '+preparation.table(name, 'facets')+
                ' WHERE source=? AND preparation_revision=? AND kind=? ORDER BY position', (source, revision, kind))
                for kind in ('genre', 'era')}
            for kind, member in units:
                if kind == 'facet':
                    actual = readers[member[0]].fetchone()
                    if actual is None or tuple(actual) != (source, revision, *member):
                        raise MigrationError('Saved recipe facet differs from its original or references')
            if any(reader.fetchone() is not None for reader in readers.values()):
                raise MigrationError('Saved recipe contains extra facets')
    finally:
        conn.rollback()


def original(name, conn, header):
    return preparation.read_original(conn, name, header)


OWNERS = {name:MappedPublication(name, preparation.JOBS[name], preparation.table(name, 'generations'),
    preparation.table(name, 'heads'), playlist_recipe_state.available, partial(verify_document, name),
    partial(original, name)) for name in ('recipes', 'canvases')}


def repair(name, conn, source):
    if conn.in_transaction:
        raise MigrationError('Saved document recovery requires an idle connection')
    header = preparation.head(conn, name, source)
    row = source_row(conn, name, source)
    if header is None or row is None:
        raise MigrationError('Saved document recovery requires its retained source')
    raw = bytes(row['document']) if row['document'] is not None else b''
    if hashlib.sha256(raw).hexdigest() != header['raw_digest'] or row['created_at'] != header['created_at']:
        raise MigrationError('Saved document changed; reconcile before file recovery')
    plan = preparation.prepare(name, conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt saved document differs from its retained manifest')


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    import json
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=tuple(OWNERS))
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-source')
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=128)
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 128:
        parser.error('--batches requires 1 to 128 batches')
    owner = OWNERS[args.job]
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.publish_stage is not None:
                owner.publish(conn, stage=args.publish_stage)
                print(json.dumps({'job':args.job, 'selected':owner.selected(conn)}))
            elif args.repair_source is not None:
                repair(args.job, conn, args.repair_source)
                print(json.dumps({'job':args.job, 'repaired':args.repair_source}))
            else:
                from dataclasses import asdict
                for _ in range(args.batches):
                    stage = owner.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                print(json.dumps(asdict(stage)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Saved document publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
