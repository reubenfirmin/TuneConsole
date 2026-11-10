"""Prepare legacy GC and Spotify receipts without admitting remote operations."""
import hashlib
import json
from pathlib import Path

from .. import collection_recovery, playlist_scopes, policy_documents, recommendation_outputs as files, workflow_settings
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from ..recommendation_work import finite
from . import document_plan
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
PREFIXES = {'gc': workflow_settings.GC, 'playlists': workflow_settings.PLAYLIST, 'albums': workflow_settings.ALBUM}


def table(name, suffix):
    return 'prepared_workflow_' + name + '_' + suffix


def number(field, required=False):
    finite_sql = f"typeof({field}) IN ('integer','real') AND abs({field})<=1.7976931348623157e308"
    return f'{field} REAL ' + (f'NOT NULL CHECK({finite_sql})' if required else f'CHECK({field} IS NULL OR ({finite_sql}))')


def definitions(name):
    head = table(name, 'generations')
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')), raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),
        raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_SOURCE_BYTES}), part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512),
        member_count INTEGER NOT NULL CHECK(member_count>=0), unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision)) WITHOUT ROWID'''
    current = table(name, 'heads')
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY, preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    specs = {'parts': (f'''position INTEGER NOT NULL CHECK(position>=0), filename TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64), byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES})''', 'position', '')}
    custody = f'''recovery_filename TEXT NOT NULL, recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER NOT NULL CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES})'''
    if name == 'gc':
        specs['attempts'] = (f'''captured_account_id INTEGER NOT NULL, account_id INTEGER,
            playlist_scope_id TEXT, provider_playlist_id_snapshot TEXT NOT NULL CHECK(length(provider_playlist_id_snapshot)>0),
            resolution_reason TEXT CHECK(resolution_reason IN ('missing_account','missing_scope')),
            attempts INTEGER NOT NULL CHECK(typeof(attempts)='integer' AND attempts>=1),
            {number('last_attempt_at', True)}, {number('next_retry_at', True)},
            phase TEXT CHECK(phase IN ('reconcile','backup','delete')), failure_class TEXT, error TEXT, {custody},
            CHECK(account_id IS NULL OR account_id=captured_account_id),
            CHECK((playlist_scope_id IS NULL)=(resolution_reason IS NOT NULL))''', 'captured_account_id,provider_playlist_id_snapshot', '')
    else:
        specs['receipts'] = (f'''position INTEGER NOT NULL CHECK(position>=0),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('playlist','album')), captured_account_id INTEGER, account_id INTEGER,
            fingerprint TEXT NOT NULL CHECK(length(fingerprint)>0), duplicate_position INTEGER CHECK(duplicate_position>=0 AND duplicate_position<position),
            creation_phase TEXT NOT NULL CHECK(creation_phase IN ('legacy','prepared','creating','created')), marker TEXT,
            playlist_scope_id TEXT, provider_playlist_id_snapshot TEXT,
            resolution_reason TEXT CHECK(resolution_reason IN ('missing_account','unrecorded_account','missing_scope')),
            complete INTEGER CHECK(complete IN (0,1)), order_verified INTEGER CHECK(order_verified IN (0,1)),
            {number('updated_at')}, last_run_added INTEGER CHECK(last_run_added IS NULL OR (typeof(last_run_added)='integer' AND last_run_added>=0)),
            {custody}, CHECK(account_id IS NULL OR account_id=captured_account_id),
            CHECK(creation_phase<>'creating' OR marker IS NOT NULL),
            CHECK(creation_phase<>'created' OR provider_playlist_id_snapshot IS NOT NULL)''', 'position', '')
        specs['items'] = ('''receipt_position INTEGER NOT NULL CHECK(receipt_position>=0), position INTEGER NOT NULL CHECK(position>=0),
            video_id_snapshot TEXT CHECK(video_id_snapshot IS NULL OR length(video_id_snapshot)>0)''', 'receipt_position,position',
            f', FOREIGN KEY(source,preparation_revision,receipt_position) REFERENCES {table(name,"receipts")}(source,preparation_revision,position)')
    for suffix, (columns, key, foreign) in specs.items():
        target = table(name, suffix)
        yield target, f'''CREATE TABLE {target} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, {columns},
            PRIMARY KEY(source,preparation_revision,{key}),
            FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision){foreign}) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Workflow preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'workflow-receipts'


def integer(value, *, nullable=False, minimum=-(2 ** 63)):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value < 2 ** 63:
        raise ValueError('Workflow identity/counter requires an in-range integer')
    return value


def optional_text(value):
    if value is not None and not isinstance(value, str):
        raise ValueError('Workflow field requires text or null')
    return value


def optional_bool(value):
    if value is not None and (type(value) not in (bool, int) or value not in (0, 1)):
        raise ValueError('Workflow flag requires a boolean or null')
    return int(value) if value is not None else None


def target(conn, account, remote):
    known = conn.execute('SELECT id FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
    if not known:
        return None, None, 'unrecorded_account' if account is None else 'missing_account'
    if remote is None:
        return known[0], None, None
    scope = playlist_scopes.resolve(conn, remote, account)
    observed = conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (scope,)).fetchone()
    return known[0], observed[0] if observed else None, None if observed else 'missing_scope'


def recovery(root, kind, owner, value):
    encoded = canonical({'format': collection_recovery.FORMAT, 'kind': kind, 'owner': owner, 'state': value})
    if len(encoded) > collection_recovery.MAX_BYTES:
        raise ValueError('Workflow recovery exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest, len(encoded)


def gc_units(conn, root, source, value, fields, *, retain=None):
    retain = retain or recovery
    account, remote = workflow_settings._gc_owner(source)
    integer(account)
    if value is None:
        return
    if (not isinstance(value, dict) or type(value.get('identity')) is not int
            or value['identity'] != account or value.get('ytm') != remote
            or value.get('phase') not in (None, 'reconcile', 'backup', 'delete')):
        raise ValueError('GC receipt disagrees with its captured target or phase')
    known, scope, reason = target(conn, account, remote)
    fields['member_count'] = 1
    yield 'gc', (account, known, scope, remote, reason, integer(value['attempts'], minimum=1),
        finite(value['last_attempt_at']), finite(value['next_retry_at']), value.get('phase'),
        optional_text(value.get('failure_class')), optional_text(value.get('error')), retain(root, 'playlist-gc', source, value))


def receipt_units(conn, root, source, value, fields, *, retain=None):
    retain = retain or recovery
    kind, account = workflow_settings.receipt_key(source)
    integer(account, nullable=True)
    if value is None:
        return
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        members = ((item, None) for item in value)
    elif isinstance(value, dict) and kind == 'playlist':
        members = value.items()
    else:
        raise ValueError('Unsupported Spotify workflow receipt family')
    seen = {}
    for position, (fingerprint, item) in enumerate(members):
        if not isinstance(fingerprint, str) or not fingerprint or not isinstance(item, (dict, type(None))):
            raise ValueError('Unsupported Spotify receipt format')
        state = dict(item or {})
        state.pop(workflow_settings.TOKEN, None)
        phase = state.get('creation_phase', 'legacy')
        marker, remote = optional_text(state.get('marker')), optional_text(state.get('ytm_playlist_id'))
        if (phase not in ('legacy', 'prepared', 'creating', 'created') or phase == 'creating' and marker is None
                or phase == 'created' and remote is None):
            raise ValueError('Unsupported Spotify creation checkpoint')
        known, scope, reason = target(conn, account, remote)
        slots = state.get('source_video_ids', [])
        if not isinstance(slots, list) or any(video is not None and (not isinstance(video, str) or not video) for video in slots):
            raise ValueError('Unsupported Spotify ordered source slots')
        owner = json.dumps([source, fingerprint], ensure_ascii=False, separators=(',', ':'))
        fields['member_count'] += 1
        yield 'receipt', (position, kind, account, known, fingerprint, seen.get(fingerprint), phase, marker, scope, remote, reason,
            optional_bool(state.get('complete')), optional_bool(state.get('order_verified')),
            finite(state['updated_at']) if state.get('updated_at') is not None else None,
            integer(state.get('last_run_added'), nullable=True, minimum=0),
            retain(root, 'spotify-transfer', owner, None if item is None else state))
        seen.setdefault(fingerprint, position)
        for index, video in enumerate(slots):
            yield 'item', (position, index, video)


def describe(name, conn, row, *, retain=None, retain_recovery=None):
    retain = retain or files.publish
    source = row['key']
    raw = bytes(row['value']) if row['value'] is not None else b''
    if len(raw) > MAX_SOURCE_BYTES:
        raise MigrationError('Workflow source exceeds its preparation byte budget')
    text = raw.decode() if row['value'] is not None else None
    value = json.loads(text) if text else None
    fields = {'source_kind': 'null' if text is None else 'blank' if text == '' else 'recorded',
              'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
              'part_count': max(1, (len(text or '') + PART_CHARACTERS - 1) // PART_CHARACTERS), 'member_count': 0}
    root = directory(conn)
    def units():
        for position in range(fields['part_count']):
            part = None if text is None else text[position * PART_CHARACTERS:(position + 1) * PART_CHARACTERS]
            yield 'part', (position, *retain(root, files.encode(source, 'original:' + str(position), part)))
        members = (gc_units(conn, root, source, value, fields, retain=retain_recovery) if name == 'gc'
                   else receipt_units(conn, root, source, value, fields, retain=retain_recovery))
        for kind, values in members:
            # File references are planned as a tuple and stored as SQL columns.
            yield kind, (*values[:-1], *values[-1]) if kind in ('gc', 'receipt') else values
    return fields, units()


def prepare(name, conn, row, *, revision, offset, rebuild=False):
    source = row['key']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(name,"generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared workflow has no durable plan')
        return document_plan.load(source, previous)
    fields, units = describe(name, conn, row)
    result = document_plan.build(directory(conn), source, fields, units)
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt workflow plan differs from its captured checkpoint')
    return result


def begin(name, conn, row, revision, plan):
    values = {'source': row['key'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(name,"generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(name, conn, row, revision, position, unit):
    kind, values = unit
    suffix = {'part': 'parts', 'gc': 'attempts', 'receipt': 'receipts', 'item': 'items'}[kind]
    conn.execute(f'INSERT INTO {table(name,suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})',
                 (row['key'], revision, *values))


def complete(name, conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(name,"heads")} VALUES (?,?) ON CONFLICT(source) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['key'], revision))


def delete(name, conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table(name,"heads")} WHERE source=?', (source,))


def resume_read(conn, owner, key):
    source, = json.loads(key)
    return conn.execute('SELECT key FROM main.settings WHERE key=? AND json_array(key)=?', (source, key)).fetchone()


def head(conn, name, source):
    return conn.execute(f'SELECT g.* FROM {table(name,"heads")} h JOIN {table(name,"generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (source,)).fetchone()


def read_original(conn, name, generation):
    source = generation['source']
    rows = list(conn.execute(f'SELECT * FROM {table(name,"parts")} WHERE source=? AND preparation_revision=? ORDER BY position',
                            (source, generation['preparation_revision'])))
    if len(rows) != generation['part_count'] or [row['position'] for row in rows] != list(range(len(rows))):
        raise MigrationError('Prepared workflow original has incomplete parts')
    parts = [files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                        chunk_key='original:' + str(row['position'])) for row in rows]
    text = None if generation['source_kind'] == 'null' else ''.join(parts)
    if text is None and parts != [None]:
        raise MigrationError('Prepared workflow has invalid null source')
    raw = text.encode() if text is not None else b''
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared workflow differs from its captured bytes')
    return text


def definition(name):
    prefix = PREFIXES[name]
    predicate = f'substr({{prefix}}key,1,{len(prefix)})={prefix!r}' if name == 'gc' else (
        f'({{prefix}}key={prefix!r} OR substr({{prefix}}key,1,{len(prefix)+1})={prefix+":"!r})')
    return Job('workflow_' + name, 79, 97, {'settings': ('key',)}, tuple(definitions(name)), None, None,
        dependencies=('identities', 'playlist_scopes'), refresh_highwater=False, filters={'settings': predicate},
        projections={'settings': ('key', f'CASE WHEN value IS NULL THEN NULL ELSE '
                     f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_SOURCE_BYTES+1}),X'') END AS value")})


def job(name):
    from functools import partial
    return OrderedJob(definition(name), *(partial(method, name) for method in (prepare, begin, copy, complete, delete)),
                      revision_aware=True, resume_read=resume_read)


JOBS = {name: job(name) for name in PREFIXES}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
