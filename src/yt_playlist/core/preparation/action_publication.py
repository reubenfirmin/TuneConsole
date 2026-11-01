"""Select verified historical action custody without admitting remote execution."""
import hashlib
import json
from pathlib import Path

from .. import action_backup_files, action_work, collection_recovery, policy_documents
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from . import action_ledger as preparation, document_plan
from .mapped_publication import MappedPublication


KEYS = {'parts':('position',), 'targets':('role','position'), 'backups':('position',),
        'steps':('position',), 'placements':('target_role','target_position'),
        'placement_items':('position',), 'placement_handles':('video_id_snapshot',),
        'resolutions':('list_kind','position')}


def source_row(conn, source):
    row = conn.execute('SELECT '+','.join(preparation.DEFINITION.projections['actions'])+
        ' FROM main.actions WHERE id=?', (source,)).fetchone()
    return dict(row) if row is not None else None


def source_original(row):
    return {('id' if key == 'action_id' else key):
        (bytes(value).decode() if key.endswith('_json') and value is not None else value)
        for key, value in row.items() if key != 'source'}


def retained(root, encoded, digest):
    if not 0 < len(encoded) <= action_backup_files.MAX_BYTES or hashlib.sha256(encoded).hexdigest() != digest:
        raise MigrationError('Action custody exceeds its budget or differs from its digest')
    path = Path(root) / digest[:2] / (digest+'.json')
    with path.open('rb') as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Action custody differs from its original')
    return str(path)


def retained_original(root, encoded):
    digest = hashlib.sha256(encoded).hexdigest()
    return retained(root, encoded, digest), digest, len(encoded)


def retained_recovery(root, kind, owner, value):
    encoded = canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':str(owner), 'state':value})
    if len(encoded) > collection_recovery.MAX_BYTES:
        raise MigrationError('Action recovery exceeds its byte budget')
    return retained_original(root, encoded)


def backup_reader(conn, header, *, repair_missing=False):
    def read(filename):
        if filename is None:
            return (None,None,None,'none'), None, None
        prior = conn.execute('SELECT * FROM '+preparation.table('backups')+
            ' WHERE source=? AND preparation_revision=? AND backup_filename=?',
            (header['source'], header['preparation_revision'], filename)).fetchone()
        if prior is None:
            raise MigrationError('Action has no captured backup reference')
        current, value = action_work.backup_reference(filename, legacy=True)
        reference = (filename, prior['backup_digest'], prior['backup_bytes'], prior['backup_status'])
        # The original can disappear after custody was established. A changed
        # original, or a newly appearing historical backup, requires reconciliation.
        if current != reference and not (current[3] == 'missing_historical' and reference[3] == 'verified'):
            raise MigrationError('Action external backup changed; reconcile its source')
        if reference[3] != 'verified':
            return reference, None, None
        try:
            encoded = action_backup_files.verified(prior['retained_filename'], reference[1], reference[2])[0]
        except (OSError, ValueError):
            if not repair_missing or current != reference:
                raise
            encoded = action_backup_files.verified(filename, reference[1], reference[2])[0]
        if value is None:
            value = json.loads(encoded)
        return reference, value, encoded
    return read


def verify_document(conn, header):
    if conn.in_transaction:
        raise MigrationError('Action verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(conn, source)
    original = preparation.read_original(conn, header)
    if row is None or original != source_original(row):
        raise MigrationError('Action original differs from its installed source')
    fields, units = preparation.describe(conn, row, revision=revision, retain=retained,
        retain_recovery=retained_recovery, retain_original=retained_original,
        read_backup=backup_reader(conn, header), source_sequence=header['source_sequence'])
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename','plan_digest','plan_bytes')))
    counts, count = dict.fromkeys(KEYS, 0), 0
    for position, (suffix, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((suffix, values)):
            raise MigrationError('Action plan differs from its original or references')
        keys = KEYS[suffix]
        actual = conn.execute('SELECT * FROM '+preparation.table(suffix)+
            ' WHERE source=? AND preparation_revision=? AND '+' AND '.join(key+'=?' for key in keys),
            (source, revision, *values[:len(keys)])).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(values):
            raise MigrationError('Action typed member differs from its original')
        counts[suffix] += 1
        count += 1
    fields['unit_count'] = count
    expected = dict(source=source, preparation_revision=revision, **fields,
        **{key:header[key] for key in ('plan_filename','plan_digest','plan_bytes')})
    if canonical(plan.fields) != canonical(fields) or len(plan.units) != count or dict(header) != expected:
        raise MigrationError('Action fields differ from their captured plan or references')
    for suffix, expected_count in counts.items():
        if conn.execute('SELECT count(*) FROM '+preparation.table(suffix)+
                ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0] != expected_count:
            raise MigrationError('Action contains missing or extra typed members')


class ActionPublication(MappedPublication):
    def proof(self, conn):
        proof = super().proof(conn)
        sequence = conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name='actions'").fetchone()
        # sqlite_sequence cannot have capture triggers. Read it with each SQL
        # proof, including under the final writer reservation, and never write it.
        return (*proof, tuple(sequence) if sequence is not None else None)

    def verify(self, conn, identity):
        captured = super().verify(conn, identity)
        relation = self.table('map')+' m JOIN '+self.header+' g ON g.source=m.source AND g.preparation_revision=m.generation'
        if conn.execute('SELECT 1 FROM '+relation+" WHERE m.stage=? AND g.kind='merge_draft' "
                'GROUP BY g.draft_signature HAVING count(*)>1 LIMIT 1', (identity,)).fetchone():
            raise MigrationError('Action publication contains duplicate draft signatures')
        if conn.execute('SELECT 1 FROM '+relation+' WHERE m.stage=? AND g.undo_of_action_id IS NOT NULL AND '
                'NOT EXISTS (SELECT 1 FROM '+self.table('map')+' linked WHERE linked.stage=m.stage '
                'AND linked.source=CAST(g.undo_of_action_id AS TEXT)) LIMIT 1', (identity,)).fetchone():
            raise MigrationError('Action publication omits a known undo target')
        return captured


OWNER = ActionPublication('action-ledger', preparation.JOB, preparation.table('generations'),
    preparation.table('heads'), action_work.available, verify_document, preparation.read_original)


def members(conn, header, suffix, order):
    return conn.execute('SELECT * FROM '+preparation.table(suffix)+
        ' WHERE source=? AND preparation_revision=? ORDER BY '+order,
        (header['source'], header['preparation_revision'])).fetchall()


def document(conn, header):
    value = collection_recovery.read(*(header[key] for key in ('recovery_filename','recovery_digest','recovery_bytes')),
        kind='action', owner=header['source'])
    if header['kind'] == 'merge_draft' or value['undo'] is None:
        return value
    undo = json.loads(value['undo'])
    if not isinstance(undo, dict):
        return value
    backups = {row['backup_filename']:row['retained_filename'] for row in members(conn, header, 'backups', 'position')
               if row['backup_status'] == 'verified'}
    changed = False
    def relocate(entry, field):
        nonlocal changed
        if entry.get(field) in backups:
            entry[field] = backups[entry[field]]
            changed = True
    relocate(undo, 'backup')
    if 'backups' in undo and any(filename in backups for filename in undo['backups']):
        undo['backups'] = [backups.get(filename, filename) for filename in undo['backups']]
        changed = True
    journal = undo.get('recovery')
    if isinstance(journal, dict) and journal.get('version') == 1:
        journal['steps'].sort(key=lambda step:step['kind'] != 'backup')
        journal['version'] = 2
        changed = True
    for row in members(conn, header, 'steps', 'position'):
        if row['operation'] == 'place_tracks':
            continue
        if row['operation'] == 'delete_playlist':
            entry = undo['deletions'][row['position']]
        elif row['operation'] == 'merge_plan':
            entry = undo['restored'][row['position']-500000]
        else:
            entry = journal['steps'][row['position']-1000000]
        if entry.get('phase') != row['phase']:
            entry['phase'] = row['phase']
            changed = True
        relocate(entry, 'backup')
        relocate(entry, 'deletion_backup')
    if changed:
        value['undo'] = json.dumps(undo)
    return value


@collection_recovery.pinned_read
def records(conn, where='', values=(), *, order='action_id DESC', limit=None):
    selected = OWNER.selected(conn)
    if selected is None:
        return None
    headers = conn.execute('SELECT g.* FROM '+OWNER.table('map')+' m JOIN '+OWNER.header+
        ' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=?'+
        (' AND ('+where+')' if where else '')+' ORDER BY '+order+(' LIMIT ?' if limit is not None else ''),
        (selected, *values, *((limit,) if limit is not None else ()))).fetchall()
    return [(header, document(conn, header)) for header in headers]


@collection_recovery.pinned_read
def legacy_undo(conn, row):
    """Keep established backup custody usable after unrelated SQL writes.

    This projects only an exactly unchanged undo document from the last selected
    map. Current action fields and reference ownership still come from SQL.
    """
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (OWNER.table('head'),)).fetchone():
        return row['undo_json']
    header = conn.execute('SELECT g.* FROM '+OWNER.table('head')+' h JOIN '+OWNER.table('map')+
        ' m ON m.stage=h.revision AND m.source=? JOIN '+OWNER.header+
        ' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE h.name=?',
        (str(row['id']), OWNER.name)).fetchone()
    if header is None or header['kind'] != row['kind'] or header['created_at'] != row['created_at']:
        return row['undo_json']
    captured = collection_recovery.read(*(header[key] for key in ('recovery_filename','recovery_digest','recovery_bytes')),
        kind='action', owner=header['source'])
    if captured['undo'] != row['undo_json']:
        return row['undo_json']
    return document(conn, header)['undo']


def repair(conn, source):
    if conn.in_transaction:
        raise MigrationError('Action file recovery requires an idle connection')
    header, row = preparation.head(conn, source), source_row(conn, source)
    if header is None or row is None:
        raise MigrationError('Action file recovery requires its retained source')
    original = source_original(row)
    if hashlib.sha256(canonical(original)).hexdigest() != header['raw_digest']:
        raise MigrationError('Action source changed; reconcile before recovery')
    # Recreate only exact captured bytes, from retained custody or a matching
    # original backup. Loss of both copies requires explicit restoration.
    fields, units = preparation.describe(conn, row, revision=header['preparation_revision'],
        read_backup=backup_reader(conn, header, repair_missing=True), source_sequence=header['source_sequence'])
    plan = document_plan.build(preparation.directory(conn), source, fields, units)
    if plan.manifest != tuple(header[key] for key in ('plan_filename','plan_digest','plan_bytes')):
        raise MigrationError('Rebuilt action differs from its captured manifest')


def validate_installed(conn):
    OWNER.validate_installed(conn)


def main(argv=None):
    import argparse
    from contextlib import closing
    from dataclasses import asdict
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish-stage', type=int)
    action.add_argument('--repair-source')
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
            elif args.repair_source is not None:
                repair(conn, args.repair_source)
                print(json.dumps({'repaired':args.repair_source}))
            else:
                for _ in range(args.batches):
                    stage = OWNER.step(conn, batch_size=args.batch_size)
                    if stage.complete:
                        break
                print(json.dumps(asdict(stage)))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Action publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
