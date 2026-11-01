"""Prepare TV and Radio operation custody without admitting remote work."""
import hashlib
from itertools import chain
import json
from pathlib import Path

from .. import collection_recovery, recommendation_outputs as files
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from ..recommendation_work import finite
from . import document_plan
from .ordered_rows import OrderedJob
from .rows import Job, main as run
from .workflow_receipts import integer, number, optional_text, recovery


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
# Distinct custody/checkpoints survive historical owner replacement. A job for
# the newly installed owner never pretends the old capture triggers survived.
SOURCES = {
    'tv_legacy': ('rec_recipes', 'playlist_ytm', 79, 96),
    'tv_native': ('rec_recipes', 'id', 97, 97),
    'radio_legacy': ('settings', 'key', 79, 93),
    'radio_native': ('radio_sessions', 'id', 94, 97),
}


def table(name, suffix):
    return 'prepared_append_' + name + '_' + suffix


def definitions(name):
    head = table(name, 'generations')
    specific = (f'''stream_token TEXT, request_id TEXT, state TEXT CHECK(state IN ('pending','complete')),
        {number('started_at')}, role_mask INTEGER NOT NULL CHECK(role_mask BETWEEN 0 AND 7),
        CHECK((operation_present=0)=(state IS NULL)),
        CHECK(operation_present=0 OR (stream_token IS NOT NULL AND request_id IS NOT NULL))'''
        if name.startswith('tv_') else f'''episode_id TEXT, captured_account_id INTEGER, account_id INTEGER,
        account_resolution TEXT CHECK(account_resolution IN ('unrecorded_account','missing_account')),
        kind TEXT CHECK(kind IN ('append','replacement')), sent INTEGER CHECK(sent IN (0,1)),
        {number('started_at')}, receipt_status TEXT, receipt_video_id_snapshot TEXT, receipt_handle_snapshot TEXT,
        allowed_count INTEGER NOT NULL CHECK(allowed_count>=0), waiting_present INTEGER NOT NULL CHECK(waiting_present IN (0,1)),
        play_prefix_present INTEGER NOT NULL CHECK(play_prefix_present IN (0,1)), adding_video_id_snapshot TEXT,
        slot INTEGER CHECK(slot IS NULL OR (typeof(slot)='integer' AND slot>=0)), choice_id TEXT,
        CHECK((operation_present=0)=(kind IS NULL)), CHECK(operation_present=0 OR sent IS NOT NULL),
        CHECK(account_id IS NULL OR account_id=captured_account_id)''')
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        entity_id INTEGER, source_revision BLOB CHECK(source_revision IS NULL OR typeof(source_revision) IN ('integer','text')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded','recovery')),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512),
        member_count INTEGER NOT NULL CHECK(member_count>=0), unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        captured_scope_ref TEXT, scope_ref TEXT, scope_resolution TEXT CHECK(scope_resolution='missing_scope'),
        operation_present INTEGER NOT NULL CHECK(operation_present IN (0,1)),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        recovery_filename TEXT, recovery_digest TEXT CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES}),
        {specific}, PRIMARY KEY(source,preparation_revision),
        CHECK((recovery_filename IS NULL)=(recovery_digest IS NULL)),
        CHECK((recovery_filename IS NULL)=(recovery_bytes IS NULL)),
        CHECK((operation_present=0)=(recovery_filename IS NULL)),
        CHECK(scope_ref IS NULL OR scope_ref=captured_scope_ref)) WITHOUT ROWID'''
    current = table(name, 'heads')
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY, preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    parts = table(name, 'parts')
    yield parts, f'''CREATE TABLE {parts} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
        filename TEXT NOT NULL, content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    items = table(name, 'items')
    roles = "'before','additions','items'" if name.startswith('tv_') else "'allowed','confirmed','waiting','play_prefix','proposed','excluded'"
    yield items, f'''CREATE TABLE {items} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ({roles})), list_position INTEGER NOT NULL CHECK(list_position>=0),
        position INTEGER NOT NULL CHECK(position>=0), video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),
        PRIMARY KEY(source,preparation_revision,role,list_position,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''


def directory(conn, name):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Append preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / ('append-' + name)


def document(name, row):
    if name.endswith('_native'):
        kind = 'playlist-recipe' if name.startswith('tv_') else 'radio-session'
        owner = row['entity_id'] if name.startswith('tv_') else row['source']
        value = collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'], kind=kind, owner=owner)
        # Native recovery files require canonical bytes. Reconstructing their
        # verified envelope preserves those exact bytes in our separate root.
        raw = canonical({'format': collection_recovery.FORMAT, 'kind': kind, 'owner': str(owner), 'state': value})
        return raw.decode(), value, 'recovery'
    raw = bytes(row['document']) if row['document'] is not None else b''
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Append source exceeds its preparation byte budget')
    text = raw.decode() if row['document'] is not None else None
    return text, json.loads(text) if text else None, 'null' if text is None else 'blank' if text == '' else 'recorded'


def videos(items, *, metadata=False):
    if not isinstance(items, list):
        raise ValueError('Append members require an ordered list')
    for item in items:
        video = item.get('video_id') if isinstance(item, dict) else None
        if not metadata:
            video = item
        if not isinstance(video, str) or not video:
            raise ValueError('Append member requires a nonempty video ID')
        yield video


def tv_units(root, source, value, fields, *, retain=None):
    retain = retain or recovery
    fields['role_mask'] = 0
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError('TV append source requires a recipe object')
    checkpoint = value.get('tv_refill')
    if checkpoint is None:
        return
    if (not isinstance(checkpoint, dict) or not isinstance(value.get('tv_stream'), str)
            or not isinstance(checkpoint.get('request_id'), str) or checkpoint.get('state') not in ('pending', 'complete')):
        raise ValueError('Unsupported TV append checkpoint')
    fields.update(operation_present=1, stream_token=value['tv_stream'], request_id=checkpoint['request_id'], state=checkpoint['state'],
                  started_at=finite(checkpoint['at']) if checkpoint.get('at') is not None else None)
    fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), retain(root, 'tv-append', source, checkpoint)))
    for bit, role in enumerate(('before', 'additions', 'items')):
        if role not in checkpoint:
            continue
        fields['role_mask'] |= 1 << bit
        for position, video in enumerate(videos(checkpoint[role], metadata=role != 'before')):
            fields['member_count'] += 1
            yield 'item', (role, 0, position, video)


def radio_units(conn, root, source, value, fields, *, retain=None):
    retain = retain or recovery
    fields.update(allowed_count=0, waiting_present=0, play_prefix_present=0)
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError('Radio append source requires a session object')
    episode = optional_text(value.get('episode_id'))
    account = integer(value.get('identity'), nullable=True)
    known = conn.execute('SELECT id FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
    fields.update(episode_id=episode, captured_account_id=account, account_id=known[0] if known else None,
                  account_resolution=None if known else 'unrecorded_account' if account is None else 'missing_account')
    pending, replacement = value.get('pending'), value.get('replacement')
    if pending and replacement:
        raise ValueError('Radio session owns two active playlist operations')
    if not pending and not replacement:
        return
    append = bool(pending)
    operation = value if append else replacement
    if not isinstance(operation, dict):
        raise ValueError('Unsupported Radio replacement operation')
    receipt = (value.get('pending_result') if append else operation.get('receipt')) or {}
    if not isinstance(receipt, dict):
        raise ValueError('Unsupported Radio write receipt')
    allowed = [] if append else operation.get('allowed', [])
    if not isinstance(allowed, list):
        raise ValueError('Radio allowed queues require an ordered list')
    adding = next(videos([pending], metadata=True)) if append else (
        next(videos([operation['adding']], metadata=True)) if operation.get('adding') is not None else None)
    started = value.get('pending_started_at') if append else operation.get('waiting_at')
    fields.update(operation_present=1, kind='append' if append else 'replacement',
        sent=int(bool(value.get('pending_sent')) if append else operation.get('waiting') is not None),
        started_at=finite(started) if started is not None else None,
        receipt_status=optional_text(receipt.get('status')), receipt_video_id_snapshot=optional_text(receipt.get('videoId')),
        receipt_handle_snapshot=optional_text(receipt.get('setVideoId')), allowed_count=len(allowed),
        waiting_present=int(not append and operation.get('waiting') is not None),
        play_prefix_present=int(not append and operation.get('play_prefix') is not None), adding_video_id_snapshot=adding,
        slot=None if append else integer(operation.get('slot'), nullable=True, minimum=0),
        choice_id=None if append else optional_text(operation.get('choice_id')))
    fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), retain(root, 'radio-session', source, value)))
    if append:
        return
    sequences = (('allowed', index, values, False) for index, values in enumerate(allowed))
    sequences = chain(sequences, ((role, 0, operation.get(role) or [], False) for role in ('confirmed', 'waiting', 'play_prefix', 'excluded')),
                      [('proposed', 0, operation.get('proposed', []), True)])
    for role, index, values, metadata in sequences:
        for position, video in enumerate(videos(values, metadata=metadata)):
            fields['member_count'] += 1
            yield 'item', (role, index, position, video)


def describe(name, conn, row, *, retain=None, retain_recovery=None):
    retain = retain or files.publish
    source = row['source']
    text, value, kind = document(name, row)
    if name == 'radio_native' and (not isinstance(value, dict) or value.get('episode_id') != source
            or value.get('identity') != row['source_account_id']):
        raise ValueError('Radio recovery disagrees with its captured episode/account')
    raw = text.encode() if text is not None else b''
    captured_scope = row.get('captured_scope_ref')
    if name == 'radio_legacy' and isinstance(value, dict) and value.get('playlist'):
        from .. import playlist_scopes
        # Resolution is observational: a missing account/scope is not admitted.
        account = integer(value.get('identity'), nullable=True)
        known = conn.execute('SELECT 1 FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
        captured_scope = playlist_scopes.resolve(conn, value['playlist'], account) if known else None
    scope = conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (captured_scope,)).fetchone() if captured_scope is not None else None
    fields = {'entity_id': row.get('entity_id'), 'source_revision': row.get('source_revision'), 'source_kind': kind,
        'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
        'part_count': max(1, (len(text or '') + PART_CHARACTERS - 1) // PART_CHARACTERS), 'member_count': 0,
        'captured_scope_ref': captured_scope, 'scope_ref': scope[0] if scope else None,
        'scope_resolution': 'missing_scope' if captured_scope is not None and scope is None else None,
        'operation_present': 0, 'recovery_filename': None, 'recovery_digest': None, 'recovery_bytes': None}
    root = directory(conn, name)
    def units():
        for position in range(fields['part_count']):
            part = None if text is None else text[position * PART_CHARACTERS:(position + 1) * PART_CHARACTERS]
            yield 'part', (position, *retain(root, files.encode(source, 'original:' + str(position), part)))
        yield from (tv_units(root, source, value, fields, retain=retain_recovery) if name.startswith('tv_')
                    else radio_units(conn, root, source, value, fields, retain=retain_recovery))
    return fields, units()


def prepare(name, conn, row, *, revision, offset, rebuild=False):
    source = row['source']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(name,"generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared append has no durable plan')
        return document_plan.load(source, previous)
    fields, units = describe(name, conn, row)
    result = document_plan.build(directory(conn, name), source, fields, units)
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt append plan differs from its captured checkpoint')
    return result


def begin(name, conn, row, revision, plan):
    values = {'source': row['source'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(name,"generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(name, conn, row, revision, position, unit):
    kind, values = unit
    suffix = 'parts' if kind == 'part' else 'items'
    conn.execute(f'INSERT INTO {table(name,suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})', (row['source'], revision, *values))


def complete(name, conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(name,"heads")} VALUES (?,?) ON CONFLICT(source) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['source'], revision))


def delete(name, conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table(name,"heads")} WHERE source=?', (str(source),))


def resume_read(name, conn, owner, key):
    source, = json.loads(key)
    field = SOURCES[name][1]
    return conn.execute(f'SELECT CAST({field} AS TEXT) AS source FROM main.{owner} WHERE {field}=? AND json_array({field})=?', (source, key)).fetchone()


def head(conn, name, source):
    return conn.execute(f'SELECT g.* FROM {table(name,"heads")} h JOIN {table(name,"generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (str(source),)).fetchone()


def read_original(conn, name, generation):
    source = generation['source']
    rows = list(conn.execute(f'SELECT * FROM {table(name,"parts")} WHERE source=? AND preparation_revision=? ORDER BY position',
                            (source, generation['preparation_revision'])))
    if len(rows) != generation['part_count'] or [row['position'] for row in rows] != list(range(len(rows))):
        raise MigrationError('Prepared append original has incomplete parts')
    parts = [files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                        chunk_key='original:' + str(row['position'])) for row in rows]
    text = None if generation['source_kind'] == 'null' else ''.join(parts)
    if text is None and parts != [None]:
        raise MigrationError('Prepared append has an invalid null source')
    raw = text.encode() if text is not None else b''
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared append differs from its captured bytes')
    return text


def definition(name):
    owner, key, minimum, maximum = SOURCES[name]
    projection = [f'CAST({key} AS TEXT) AS source']
    if name.endswith('_native'):
        projection += ['id AS entity_id'] if name.startswith('tv_') else []
        if name == 'radio_native':
            projection += ['account_id AS source_account_id']
        projection += ['revision AS source_revision', 'playlist_scope_id AS captured_scope_ref', 'recovery_filename', 'recovery_digest', 'recovery_bytes']
    else:
        field = 'recipe' if name.startswith('tv_') else 'value'
        projection += [f"CASE WHEN {field} IS NULL THEN NULL ELSE COALESCE(substr(CAST({field} AS BLOB),1,{MAX_SOURCE_BYTES+1}),X'') END AS document"]
        if name.startswith('tv_'):
            projection += ['playlist_ytm AS captured_scope_ref']
    return Job('append_' + name, minimum, maximum, {owner: (key,)}, tuple(definitions(name)), None, None,
        dependencies=('identities', 'playlist_scopes'), refresh_highwater=False,
        filters={'settings': "{prefix}key='radio_current_session'"} if owner == 'settings' else None,
        projections={owner: tuple(projection)})


def job(name):
    from functools import partial
    return OrderedJob(definition(name), *(partial(method, name) for method in (prepare, begin, copy, complete, delete)),
                      revision_aware=True, resume_read=partial(resume_read, name))


JOBS = {name: job(name) for name in SOURCES}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
