"""Select verified TV/Radio operation custody while keeping installed writers."""
from functools import partial
import hashlib
from pathlib import Path
from .. import fao

from .. import append_work, collection_recovery, playlist_recipe_state, radio_checkpoint, radio_storage
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import append_operations as preparation, document_plan
from .mapped_publication import MappedPublication


def source_row(name, conn, source):
    job = preparation.JOBS[name].definition
    owner, key, _, _ = preparation.SOURCES[name]
    row = conn.execute('SELECT '+','.join(job.projections[owner])+' FROM main.'+owner+' WHERE '+key+'=?', (source,)).fetchone()
    return dict(row) if row is not None else None


def retained(root, encoded):
    if not 0 < len(encoded) <= collection_recovery.MAX_BYTES:
        raise MigrationError('Append file exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest+'.json')
    with fao.open_reader(path) as stream:
        if stream.read(len(encoded)+1) != encoded:
            raise MigrationError('Append file differs from its original')
    return str(path), digest, len(encoded)


def retained_recovery(root, kind, owner, value):
    return retained(root, canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':owner, 'state':value}))


def verify_document(name, conn, header):
    if conn.in_transaction:
        raise MigrationError('Append verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    row = source_row(name, conn, source)
    if row is None or preparation.read_original(conn, name, header) != preparation.document(name, row)[0]:
        raise MigrationError('Append original differs from its retained source')
    fields, units = preparation.describe(name, conn, row, retain=retained, retain_recovery=retained_recovery)
    plan = document_plan.load(source, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    counts, count = {'parts':0, 'items':0}, 0
    for position, (kind, values) in enumerate(units):
        if position >= len(plan.units) or canonical(plan.units[position]) != canonical((kind, values)):
            raise MigrationError('Append durable plan differs from its original or references')
        suffix = 'parts' if kind == 'part' else 'items'
        predicate, keys = ('position=?', values[:1]) if kind == 'part' else ('role=? AND list_position=? AND position=?', values[:3])
        actual = conn.execute('SELECT * FROM '+preparation.table(name, suffix)+
            ' WHERE source=? AND preparation_revision=? AND '+predicate, (source, revision, *keys)).fetchone()
        if actual is None or tuple(actual)[2:] != tuple(values):
            raise MigrationError('Append typed member differs from its original')
        counts[suffix] += 1
        count += 1
    fields['unit_count'] = count
    expected = dict.fromkeys(header.keys())
    expected.update(source=source, preparation_revision=revision, **fields,
        **{key:header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')})
    if (canonical(plan.fields) != canonical(fields) or len(plan.units) != count
            or any(header[key] != value for key, value in expected.items())):
        raise MigrationError('Append fields or member count differ from their original or references')
    for suffix, expected_count in counts.items():
        if conn.execute('SELECT count(*) FROM '+preparation.table(name, suffix)+
                ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0] != expected_count:
            raise MigrationError('Append contains missing or extra typed members')


def original(name, conn, header):
    return preparation.read_original(conn, name, header)


NATIVE = {'tv_legacy':playlist_recipe_state.available, 'radio_legacy':radio_storage.available,
          'tv_native':append_work.available, 'radio_native':append_work.available}
OWNERS = {name:MappedPublication(name, preparation.JOBS[name], preparation.table(name, 'generations'),
    preparation.table(name, 'heads'), NATIVE[name], partial(verify_document, name), partial(original, name))
    for name in preparation.SOURCES}


def selected_operation(conn, name, source):
    owner = OWNERS[name]
    stage = owner.selected(conn)
    header = owner.capture(conn, stage, str(source)) if stage is not None else None
    return header if header is not None and header['operation_present'] else None


def sequence(conn, name, header, role, index=0):
    return [row[0] for row in conn.execute('SELECT video_id_snapshot FROM '+preparation.table(name, 'items')+
        ' WHERE source=? AND preparation_revision=? AND role=? AND list_position=? ORDER BY position',
        (header['source'], header['preparation_revision'], role, index))]


def apply_tv(conn, source, value):
    if not isinstance(value, dict) or append_work.available(conn):
        return value
    name = 'tv_native' if playlist_recipe_state.available(conn) else 'tv_legacy'
    header = selected_operation(conn, name, source)
    if header is None:
        return value
    checkpoint = collection_recovery.read(*(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes')),
        kind='tv-append', owner=header['source'])
    checkpoint.update(request_id=header['request_id'], state=header['state'])
    if 'at' in checkpoint:
        checkpoint['at'] = header['started_at']
    for bit, role in enumerate(('before', 'additions', 'items')):
        if header['role_mask'] & (1 << bit):
            videos = sequence(conn, name, header, role)
            checkpoint[role] = videos if role == 'before' else [
                {**item, 'video_id':video} for item, video in zip(checkpoint[role], videos, strict=True)]
    value.update(tv_stream=header['stream_token'], tv_refill=checkpoint)
    return value


def apply_radio(conn, source, value):
    if not isinstance(value, dict) or append_work.available(conn):
        return value
    name = 'radio_native' if radio_storage.available(conn) else 'radio_legacy'
    header = selected_operation(conn, name, source)
    if header is None:
        return value
    captured = collection_recovery.read(*(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes')),
        kind='radio-session', owner=header['source'])
    append = header['kind'] == 'append'
    operation = captured if append else captured['replacement']
    fields = (('pending_sent', 'sent'), ('pending_started_at', 'started_at')) if append else (
        ('waiting_at', 'started_at'), ('slot', 'slot'))
    for field, column in fields:
        if field in operation:
            operation[field] = bool(header[column]) if field == 'pending_sent' else header[column]
    receipt = operation.get('pending_result' if append else 'receipt')
    if receipt is not None:
        for field, column in (('status', 'receipt_status'), ('videoId', 'receipt_video_id_snapshot'), ('setVideoId', 'receipt_handle_snapshot')):
            if field in receipt:
                receipt[field] = header[column]
    if append:
        if operation.get('pending') is not None:
            operation['pending']['video_id'] = header['adding_video_id_snapshot']
        for field in ('pending', 'pending_sent', 'pending_started_at', 'pending_result'):
            if field in operation:
                value[field] = operation[field]
        return value
    if 'choice_id' in operation:
        operation['choice_id'] = radio_checkpoint.project_choice(operation['choice_id'], header['choice_id'])
    if 'allowed' in operation:
        operation['allowed'] = [sequence(conn, name, header, 'allowed', index) for index in range(header['allowed_count'])]
    for role in ('confirmed', 'waiting', 'play_prefix', 'excluded'):
        if role in operation and operation[role] is not None:
            operation[role] = sequence(conn, name, header, role)
    if 'proposed' in operation:
        operation['proposed'] = [{**item, 'video_id':video} for item, video in
            zip(operation['proposed'], sequence(conn, name, header, 'proposed'), strict=True)]
    if operation.get('adding') is not None:
        operation['adding']['video_id'] = header['adding_video_id_snapshot']
    value['replacement'] = operation
    return value


def repair(name, conn, source):
    if conn.in_transaction:
        raise MigrationError('Append file recovery requires an idle connection')
    header = preparation.head(conn, name, source)
    row = source_row(name, conn, source)
    if header is None or row is None:
        raise MigrationError('Append recovery requires its retained source')
    text, _, kind = preparation.document(name, row)
    raw = text.encode() if text is not None else b''
    if (hashlib.sha256(raw).hexdigest() != header['raw_digest'] or kind != header['source_kind']
            or row.get('source_revision') != header['source_revision']):
        raise MigrationError('Append source changed; reconcile before file recovery')
    plan = preparation.prepare(name, conn, row, revision=header['preparation_revision'], offset=0)
    if plan.manifest != tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt append differs from its retained manifest')


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
        parser.exit(1, f'Append publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
