"""Select retained GC and Spotify receipts without advancing remote operations."""
from functools import partial
import hashlib
from pathlib import Path

from .. import collection_recovery, workflow_settings
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import document_plan, workflow_receipts as preparation
from .mapped_publication import MappedPublication


RELATIONS = {'part':'parts', 'gc':'attempts', 'receipt':'receipts', 'item':'items'}


def source_row(conn, source):
    return conn.execute('SELECT key,CASE WHEN value IS NULL THEN NULL ELSE '
        "COALESCE(substr(CAST(value AS BLOB),1,?),X'') END AS value FROM main.settings WHERE key=?",
        (preparation.MAX_SOURCE_BYTES+1, source)).fetchone()


def retained(root, encoded):
    if not 0 < len(encoded) <= collection_recovery.MAX_BYTES:
        raise MigrationError('Workflow file exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest+'.json')
    with path.open('rb') as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Workflow file differs from its original')
    return str(path), digest, len(encoded)


def retained_recovery(root, kind, owner, value):
    return retained(root, canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':owner, 'state':value}))


def verify_document(name, conn, header):
    if conn.in_transaction:
        raise MigrationError('Workflow verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(conn, source)
    raw = bytes(row['value']).decode('utf-8') if row is not None and row['value'] is not None else None
    if row is None or preparation.read_original(conn, name, header) != raw:
        raise MigrationError('Workflow original differs from its retained source')
    fields, units = preparation.describe(name, conn, row, retain=retained, retain_recovery=retained_recovery)
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    suffixes = ('parts', 'attempts') if name == 'gc' else ('parts', 'receipts', 'items')
    columns = {suffix:conn.execute('PRAGMA table_info('+preparation.table(name, suffix)+')').fetchall() for suffix in suffixes}
    counts, count = dict.fromkeys(suffixes, 0), 0
    for position, (kind, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((kind, values)):
            raise MigrationError('Workflow durable plan differs from its original or references')
        suffix = RELATIONS[kind]
        keys = [(column[1], value) for column, value in zip(columns[suffix][2:], values) if column[5]]
        actual = conn.execute('SELECT * FROM '+preparation.table(name, suffix)+
            ' WHERE source=? AND preparation_revision=?'+''.join(' AND '+key+' IS ?' for key, _ in keys),
            (source, revision, *(value for _, value in keys))).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(values):
            raise MigrationError('Workflow typed member differs from its original or references')
        counts[suffix] += 1
        count += 1
    fields['unit_count'] = count
    if (canonical(plan.fields) != canonical(fields) or len(plan.units) != count
            or any(header[key] != value for key, value in fields.items())):
        raise MigrationError('Workflow fields or member count differ from their original')
    for suffix in suffixes:
        if conn.execute('SELECT count(*) FROM '+preparation.table(name, suffix)+
                ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0] != counts[suffix]:
            raise MigrationError('Workflow contains missing or extra typed members')


def original(name, conn, header):
    return preparation.read_original(conn, name, header)


OWNERS = {name:MappedPublication(name, preparation.JOBS[name], preparation.table(name, 'generations'),
    preparation.table(name, 'heads'), workflow_settings.available, partial(verify_document, name),
    partial(original, name)) for name in preparation.PREFIXES}


def owner_for(source):
    if source.startswith(workflow_settings.GC):
        return OWNERS['gc']
    if source == workflow_settings.PLAYLIST or source.startswith(workflow_settings.PLAYLIST+':'):
        return OWNERS['playlists']
    if source == workflow_settings.ALBUM or source.startswith(workflow_settings.ALBUM+':'):
        return OWNERS['albums']
    raise ValueError('Unknown workflow source')


def values_with_prefix(conn, prefix, selections):
    values = {}
    for owner, stage in selections:
        after = None
        while True:
            sources = conn.execute('SELECT source FROM '+owner.table('map')+
                ' WHERE stage=? AND substr(source,1,?)=?'+(' AND source>?' if after is not None else '')+
                ' ORDER BY source LIMIT 128', (stage, len(prefix), prefix)+((after,) if after is not None else ())).fetchall()
            if not sources:
                break
            values.update((source, owner.value(conn, stage, source)) for source, in sources)
            after = sources[-1][0]
    return values


def repair(name, conn, source):
    if conn.in_transaction:
        raise MigrationError('Workflow file recovery requires an idle connection')
    if owner_for(source) is not OWNERS[name]:
        raise MigrationError('Workflow recovery source belongs to another owner')
    header = preparation.head(conn, name, source)
    row = source_row(conn, source)
    if header is None or row is None:
        raise MigrationError('Workflow recovery requires its retained source')
    raw = bytes(row['value']) if row['value'] is not None else b''
    kind = 'null' if row['value'] is None else 'blank' if not raw else 'recorded'
    if hashlib.sha256(raw).hexdigest() != header['raw_digest'] or kind != header['source_kind']:
        raise MigrationError('Workflow changed; reconcile before file recovery')
    plan = preparation.prepare(name, conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt workflow differs from its retained manifest')


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import json
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
                for _ in range(args.batches):
                    stage = owner.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                print(json.dumps(asdict(stage)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Workflow publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
