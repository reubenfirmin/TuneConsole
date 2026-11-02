"""Prepare retained action history and recovery without admitting execution."""
import hashlib
import json
from pathlib import Path

from .. import action_backup_files, action_work, playlist_scopes, policy_documents, recommendation_outputs as files
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from ..recommendation_work import finite
from . import document_plan
from .ordered_rows import OrderedJob
from .rows import Job, main as run
from .workflow_receipts import integer, number, optional_bool, optional_text, recovery


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024


def table(suffix):
    return 'prepared_action_' + suffix


def definitions():
    yield table('generations'), f'''CREATE TABLE {table('generations')} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        action_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(length(trim(kind))>0),
        status TEXT NOT NULL CHECK(status IN ('draft','planned','pending','executing','executed','failed','cancelled',
            'delete_pending','delete_failed','undoing','undo_failed','undone')),
        idempotency_key TEXT NOT NULL, intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64),
        undo_of_action_id INTEGER, unresolved_undo_id INTEGER, execution_mode TEXT CHECK(execution_mode IN ('delete','merge','move','copy')),
        draft_signature TEXT, {number('created_at')}, {number('executed_at')}, source_sequence INTEGER,
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512), member_count INTEGER NOT NULL CHECK(member_count>=0),
        unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64), plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        recovery_filename TEXT NOT NULL, recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER NOT NULL CHECK(recovery_bytes BETWEEN 1 AND 16777216),
        PRIMARY KEY(source,preparation_revision), CHECK(source=CAST(action_id AS TEXT)),
        CHECK(undo_of_action_id IS NULL OR unresolved_undo_id IS NULL)) WITHOUT ROWID'''
    yield table('heads'), f'''CREATE TABLE {table('heads')} (
        source TEXT PRIMARY KEY, preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {table('generations')}(source,preparation_revision)) WITHOUT ROWID'''
    custody = 'recovery_filename TEXT NOT NULL,recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),recovery_bytes INTEGER NOT NULL CHECK(recovery_bytes BETWEEN 1 AND 16777216)'
    backup = f'''backup_filename TEXT, retained_filename TEXT, backup_digest TEXT CHECK(length(backup_digest)=64),
        backup_bytes INTEGER CHECK(backup_bytes BETWEEN 1 AND {action_backup_files.MAX_BYTES}),
        backup_status TEXT NOT NULL CHECK(backup_status IN ('none','verified','missing_historical')),
        CHECK((backup_status='none' AND backup_filename IS NULL AND retained_filename IS NULL AND backup_digest IS NULL AND backup_bytes IS NULL) OR
              (backup_status='verified' AND backup_filename IS NOT NULL AND retained_filename IS NOT NULL AND backup_digest IS NOT NULL AND backup_bytes IS NOT NULL) OR
              (backup_status='missing_historical' AND backup_filename IS NOT NULL AND retained_filename IS NULL AND backup_digest IS NULL AND backup_bytes IS NULL))'''
    specs = {
        'parts': (f'position INTEGER NOT NULL CHECK(position>=0),filename TEXT NOT NULL,content_digest TEXT NOT NULL CHECK(length(content_digest)=64),byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES})', 'position', ''),
        'targets': ('''role TEXT NOT NULL CHECK(role IN ('source','destination','keeper','deletion','recovery','placement')),
            position INTEGER NOT NULL CHECK(position>=0), account_id INTEGER, unresolved_account_id INTEGER, playlist_scope_id TEXT,
            provider_playlist_id_snapshot TEXT NOT NULL CHECK(length(provider_playlist_id_snapshot)>0),
            membership_ref_id INTEGER, unresolved_membership_ref_id INTEGER,
            resolution_reason TEXT CHECK(resolution_reason IN ('missing_account','missing_membership','unrecorded_account','missing_scope','membership_snapshot_mismatch')),
            CHECK(account_id IS NULL OR unresolved_account_id IS NULL),
            CHECK(membership_ref_id IS NULL OR unresolved_membership_ref_id IS NULL)''', 'role,position', ''),
        'backups': (f"position INTEGER NOT NULL CHECK(position>=0),{backup},CHECK(backup_status<>'none')", 'position', ''),
        'steps': (f'''position INTEGER NOT NULL CHECK(position>=0), target_role TEXT, target_position INTEGER,
            operation TEXT NOT NULL CHECK(operation IN ('delete_playlist','restore_backup','restore_keeper','delete_copy','place_tracks','merge_plan')),
            phase TEXT NOT NULL CHECK(phase IN ('prepared','pending','uncertain','failed','creating','created','restoring','deleting','remote_complete','complete','cancelled')),
            outcome TEXT NOT NULL CHECK(outcome IN ('not_sent','unknown','confirmed_present','confirmed_absent','complete')),
            creation_marker TEXT, created_provider_id_snapshot TEXT, created_playlist_scope_id TEXT,
            original_survived INTEGER CHECK(original_survived IN (0,1)),
            observed_status TEXT NOT NULL CHECK(observed_status IN ('unrecorded','unknown','present')),
            {number('updated_at')}, {custody}, {backup},
            CHECK((target_role IS NULL)=(target_position IS NULL)),
            CHECK(phase<>'uncertain' OR outcome='unknown'), CHECK(phase<>'complete' OR outcome='complete')''', 'position',
            f', FOREIGN KEY(source,preparation_revision,target_role,target_position) REFERENCES {table("targets")}(source,preparation_revision,role,position)'),
        'placements': ('target_role TEXT NOT NULL CHECK(target_role=\'placement\'),target_position INTEGER NOT NULL CHECK(target_position=0),after_video_id_snapshot TEXT,handles_present INTEGER NOT NULL CHECK(handles_present IN (0,1))', 'target_role,target_position',
            f', FOREIGN KEY(source,preparation_revision,target_role,target_position) REFERENCES {table("targets")}(source,preparation_revision,role,position)'),
        'placement_items': ('position INTEGER NOT NULL CHECK(position>=0),video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0)', 'position', ''),
        'placement_handles': ('video_id_snapshot TEXT NOT NULL CHECK(length(video_id_snapshot)>0),occurrence_handle_snapshot TEXT NOT NULL CHECK(length(occurrence_handle_snapshot)>0)', 'video_id_snapshot', ''),
        'resolutions': ('''list_kind TEXT NOT NULL CHECK(list_kind IN ('additions','unresolved')),position INTEGER NOT NULL CHECK(position>=0),
            recording_id INTEGER,unresolved_key TEXT,captured_identity_key TEXT NOT NULL,source_video_id_snapshot TEXT,target_video_id_snapshot TEXT,
            method TEXT NOT NULL CHECK(method IN ('reuse','search','unresolved')),
            CHECK((recording_id IS NOT NULL AND unresolved_key IS NULL) OR (recording_id IS NULL AND unresolved_key=captured_identity_key))''', 'list_kind,position', ''),
    }
    for suffix, (columns, key, foreign) in specs.items():
        yield table(suffix), f'''CREATE TABLE {table(suffix)} (
            source TEXT NOT NULL,preparation_revision INTEGER NOT NULL,{columns},PRIMARY KEY(source,preparation_revision,{key}),
            FOREIGN KEY(source,preparation_revision) REFERENCES {table('generations')}(source,preparation_revision){foreign}) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Action preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'action-ledger'


def target(conn, *, account=None, remote=None, membership=None):
    integer(account, nullable=True)
    integer(membership, nullable=True)
    retained = conn.execute('SELECT scope_ref,identity_id FROM main.playlist_membership_refs WHERE id=?', (membership,)).fetchone() if membership is not None else None
    scope, reason = None, None
    if retained and retained['scope_ref'] is not None:
        owner = conn.execute('SELECT provider_id FROM main.playlist_scopes WHERE ref=?', (retained['scope_ref'],)).fetchone()
        if (account is not None and account != retained['identity_id']) or (remote is not None and remote != owner[0]):
            retained, reason = None, 'membership_snapshot_mismatch'
        else:
            account, remote, scope = retained['identity_id'], owner[0], retained['scope_ref']
    elif membership is not None:
        reason = 'missing_membership'
    if remote is None:
        remote = '@unresolved-membership:' + str(membership)
    if not isinstance(remote, str) or not remote:
        raise ValueError('Action target requires a captured remote ID')
    known = conn.execute('SELECT id FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
    if not known:
        reason = reason or ('missing_account' if account is not None else 'unrecorded_account')
    elif scope is None and not remote.startswith('@unresolved-membership:'):
        candidate = playlist_scopes.resolve(conn, remote, account)
        observed = conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (candidate,)).fetchone()
        scope = observed[0] if observed else None
        if scope is None:
            reason = reason or 'missing_scope'
    return (known[0] if known else None, None if known else account, scope, remote,
            membership if retained else None, None if retained else membership, reason)


class Planner:
    def __init__(self, conn, root, source, now, revision, rebuilding, *, retain=None, retain_recovery=None, read_backup=None):
        self.conn, self.root, self.source, self.now = conn, root, source, now
        self.revision, self.rebuilding = revision, rebuilding
        self.targets, self.backups = {}, {}
        self.retain = retain or policy_documents._publish_encoded
        self.recovery = retain_recovery or recovery
        self.read_backup = read_backup

    def target(self, role, position, **values):
        key = role, position
        fields = target(self.conn, **values)
        if key in self.targets:
            if self.targets[key] != fields:
                raise ValueError('Conflicting captured action targets')
            return
        self.targets[key] = fields
        yield 'targets', (*key, *fields)

    def backup(self, filename):
        if filename not in self.backups:
            prior = self.conn.execute(f'SELECT * FROM {table("backups")} WHERE source=? AND preparation_revision=? AND backup_filename=?',
                (self.source, self.revision, filename)).fetchone() if self.rebuilding else None
            if self.read_backup is not None:
                reference, value, captured = self.read_backup(filename)
            elif prior is not None:
                reference = (filename, prior['backup_digest'], prior['backup_bytes'], prior['backup_status'])
                if reference[3] == 'verified':
                    action_backup_files.verified(prior['retained_filename'], reference[1], reference[2])
                    _, value = action_work.backup_reference(prior['retained_filename'])
                else:
                    value = None
            else:
                reference, value = action_work.backup_reference(filename, legacy=True)
            retained = None
            if reference[3] == 'verified':
                encoded = captured if self.read_backup is not None else action_backup_files.verified(
                    prior['retained_filename'] if prior else filename, reference[1], reference[2])[0]
                retained = self.retain(self.root, encoded, reference[1])
            self.backups[filename] = ((*reference[:1], retained, *reference[1:]), value)
        return self.backups[filename]

    def step(self, position, operation, value, *, target_key=None, backup=None):
        if not isinstance(value, dict):
            raise ValueError('Action step requires an object')
        phase = value.get('phase', 'pending')
        if phase == 'pending' and operation == 'delete_playlist':
            phase = 'uncertain'
        outcome = ('unknown' if phase in ('uncertain','creating','deleting') or phase == 'restoring' and value.get('observed') is None
            else 'complete' if phase == 'complete' else 'confirmed_absent' if phase == 'remote_complete' and operation in ('delete_playlist','delete_copy')
            else 'confirmed_present' if phase in ('failed','remote_complete','restoring') else 'not_sent')
        created, created_scope = optional_text(value.get('created_id')), None
        if created is not None and target_key is not None:
            account = self.targets[target_key][0]
            if account is not None:
                candidate = playlist_scopes.resolve(self.conn, created, account)
                observed = self.conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (candidate,)).fetchone()
                created_scope = observed[0] if observed else None
        owner = json.dumps([self.source, position], separators=(',', ':'))
        yield 'steps', (position, *(target_key or (None, None)), operation, phase, outcome,
            optional_text(value.get('marker')), created, created_scope, optional_bool(value.get('original_survived')),
            'unrecorded' if 'observed' not in value else 'unknown' if value['observed'] is None else 'present', self.now,
            *self.recovery(self.root, 'action-step', owner, value), *self.backup(backup)[0])

    def units(self, kind, params, plan, undo, status):
        params = params if isinstance(params, dict) else {}
        plan = plan if isinstance(plan, dict) else {}
        undo = undo if isinstance(undo, dict) else {}
        backups = list(undo.get('backups', []))
        backups.extend(entry['backup'] for entry in undo.get('restored', []) if entry.get('backup'))
        if undo.get('backup'):
            backups.append(undo['backup'])
        backups.extend(entry['backup'] for entry in undo.get('deletions', []) if entry.get('backup'))
        backups.extend(entry.get('backup') or entry['deletion_backup'] for entry in (undo.get('recovery') or {}).get('steps', [])
                       if entry.get('backup') or entry.get('deletion_backup'))
        for position, filename in enumerate(dict.fromkeys(backups)):
            yield 'backups', (position, *self.backup(filename)[0])
        for role, field in (('source', 'source'), ('destination', 'target')):
            if type(params.get(field)) is int:
                yield from self.target(role, 0, membership=params[field], remote=params.get('source_ytm') if role == 'source' else None)
        if type(plan.get('playlist_id')) is int:
            yield from self.target('placement', 0, membership=plan['playlist_id'])
            videos, handles = plan.get('video_ids', []), plan.get('set_video_ids', {})
            if (not isinstance(videos, list) or any(not isinstance(video, str) or not video for video in videos)
                    or not isinstance(handles, dict) or any(not isinstance(video, str) or not video or not isinstance(handle, str) or not handle for video, handle in handles.items())):
                raise ValueError('Unsupported ordered action placement')
            yield 'placements', ('placement', 0, optional_text(plan.get('after_video_id')), int('set_video_ids' in plan))
            for position, video in enumerate(videos):
                yield 'placement_items', (position, video)
            for video, handle in handles.items():
                yield 'placement_handles', (video, handle)
            phase = 'complete' if status == 'executed' else 'cancelled' if status == 'cancelled' else 'pending'
            yield from self.step(750000, 'place_tracks', {'phase': phase}, target_key=('placement', 0))
        if kind == 'plan':
            for list_kind in ('additions', 'unresolved'):
                for position, item in enumerate(plan.get(list_kind, [])):
                    if not isinstance(item, dict) or set(item) != {'identity_key','source_video_id','target_video_id','method'}:
                        raise ValueError('Unsupported merge plan resolution')
                    captured = item['identity_key']
                    if not isinstance(captured, str):
                        raise ValueError('Merge plan requires a captured recording key')
                    recording = self.conn.execute('SELECT id FROM main.recordings WHERE identity_key=?', (captured,)).fetchone()
                    yield 'resolutions', (list_kind, position, recording[0] if recording else None, None if recording else captured, captured,
                        optional_text(item['source_video_id']), optional_text(item['target_video_id']), item['method'])
        for position, entry in enumerate(undo.get('restored', [])):
            yield from self.target('keeper', position, account=entry.get('identity'), remote=entry.get('ytm'))
            if entry.get('backup'):
                yield from self.step(500000+position, 'merge_plan', entry, target_key=('keeper', position), backup=entry['backup'])
        if undo.get('new_ytm') is not None:
            yield from self.target('destination', 0, account=undo.get('target_identity'), remote=undo['new_ytm'])
        for position, entry in enumerate(undo.get('deletions', [])):
            yield from self.target('deletion', position, account=entry.get('identity'), remote=entry.get('ytm'))
            yield from self.step(position, 'delete_playlist', entry, target_key=('deletion', position), backup=entry.get('backup'))
        journal = undo.get('recovery')
        if journal is not None:
            if not isinstance(journal, dict) or journal.get('version') not in (1, 2) or not isinstance(journal.get('steps'), list):
                raise ValueError('Unsupported action undo journal')
            entries = sorted(journal['steps'], key=lambda step: step['kind'] != 'backup') if journal['version'] == 1 else journal['steps']
            for position, entry in enumerate(entries):
                operation = action_work.OPERATIONS.get(entry.get('kind'))
                if operation is None:
                    raise ValueError('Unsupported action undo operation')
                account = remote = None
                backup = entry.get('backup') if operation == 'restore_backup' else entry.get('deletion_backup') if operation == 'delete_copy' else None
                if operation == 'restore_keeper':
                    account, remote = entry['entry'].get('identity'), entry['entry'].get('ytm')
                elif operation == 'delete_copy':
                    account, remote = entry.get('identity'), entry.get('ytm')
                elif backup:
                    _, captured = self.backup(backup)
                    if captured:
                        account, remote = captured.get('identity_id'), captured.get('ytm_playlist_id')
                target_key = ('recovery', position) if remote is not None else None
                if target_key is not None:
                    yield from self.target(*target_key, account=account, remote=remote)
                yield from self.step(1000000+position, operation, entry, target_key=target_key, backup=backup)


def prepare(conn, row, *, revision, offset, rebuild=False):
    source = row['source']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table("generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared action has no durable plan')
        return document_plan.load(source, previous)
    fields, units = describe(conn, row, revision=revision, rebuilding=bool(offset and rebuild))
    result = document_plan.build(directory(conn), source, fields, units)
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt action plan differs from its captured checkpoint')
    return result


def describe(conn, row, *, revision, rebuilding=False, retain=None, retain_recovery=None, retain_original=None,
             read_backup=None, source_sequence=...):
    source = row['source']
    retain_recovery = retain_recovery or recovery
    retain_original = retain_original or files.publish
    raw_fields = {}
    for field in ('params_json', 'plan_json', 'undo_json'):
        raw = bytes(row[field]) if row[field] is not None else None
        if raw is not None and len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('Action document exceeds its preparation byte budget')
        raw_fields[field] = raw.decode() if raw is not None else None
    kind, status = row['kind'], row['status']
    draft = kind == 'merge_draft'
    params = action_work.decode(raw_fields['params_json'])
    plan = action_work.decode(raw_fields['plan_json'], draft=draft)
    undo = action_work.decode(raw_fields['undo_json'], draft=draft)
    raw = canonical({'id': row['action_id'], 'kind': kind, 'status': status, 'created_at': row['created_at'], 'executed_at': row['executed_at'], **raw_fields})
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Action source exceeds its preparation byte budget')
    undo_id = integer(params.get('undid'), nullable=True) if kind == 'undo' and isinstance(params, dict) else None
    if undo_id == row['action_id'] or draft and (status != 'draft' or plan is None):
        raise ValueError('Unsupported action self-link or draft state')
    observed = conn.execute('SELECT id FROM main.actions WHERE id=?', (undo_id,)).fetchone() if undo_id is not None else None
    if source_sequence is ...:
        sequence = conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name='actions'").fetchone()
        source_sequence = sequence[0] if sequence else None
    fields = {'action_id': row['action_id'], 'kind': kind, 'status': status, 'idempotency_key': 'legacy-action:' + source,
        'intent_digest': hashlib.sha256(canonical({'kind': kind, 'params': params, 'plan': plan})).hexdigest(),
        'undo_of_action_id': observed[0] if observed else None, 'unresolved_undo_id': None if observed else undo_id,
        'execution_mode': params.get('mode') if kind == 'plan' and isinstance(params, dict) else None, 'draft_signature': plan if draft else None,
        'created_at': finite(row['created_at']) if row['created_at'] is not None else None,
        'executed_at': finite(row['executed_at']) if row['executed_at'] is not None else None,
        'source_sequence': source_sequence, 'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
        'part_count': max(1, (len(raw.decode()) + PART_CHARACTERS-1)//PART_CHARACTERS), 'member_count': 0}
    root = directory(conn)
    captured = {'params': raw_fields['params_json'], 'plan': raw_fields['plan_json'], 'undo': raw_fields['undo_json']}
    fields.update(zip(('recovery_filename','recovery_digest','recovery_bytes'), retain_recovery(root, 'action', source, captured)))
    text = raw.decode()
    planner = Planner(conn, root, source, fields['executed_at'], revision, rebuilding,
        retain=retain, retain_recovery=retain_recovery, read_backup=read_backup)
    def units():
        for position in range(fields['part_count']):
            yield 'parts', (position, *retain_original(root, files.encode(source, 'original:' + str(position), text[position*PART_CHARACTERS:(position+1)*PART_CHARACTERS])))
        if not draft:
            for unit in planner.units(kind, params, plan, undo, status):
                fields['member_count'] += 1
                yield unit
    return fields, units()


def begin(conn, row, revision, plan):
    values = {'source': row['source'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table("generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(conn, row, revision, position, unit):
    suffix, values = unit
    if suffix not in {'parts','targets','backups','steps','placements','placement_items','placement_handles','resolutions'}:
        raise MigrationError('Unknown prepared action member')
    conn.execute(f'INSERT INTO {table(suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})', (row['source'], revision, *values))


def complete(conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table("heads")} VALUES (?,?) ON CONFLICT(source) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['source'], revision))


def delete(conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table("heads")} WHERE source=?', (str(source),))


def resume_read(conn, owner, key):
    identity, = json.loads(key)
    return conn.execute('SELECT CAST(id AS TEXT) AS source FROM main.actions WHERE id=? AND json_array(id)=?', (identity, key)).fetchone()


def head(conn, identity):
    return conn.execute(f'SELECT g.* FROM {table("heads")} h JOIN {table("generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (str(identity),)).fetchone()


def read_original(conn, generation):
    source = generation['source']
    rows = list(conn.execute(f'SELECT * FROM {table("parts")} WHERE source=? AND preparation_revision=? ORDER BY position',
        (source, generation['preparation_revision'])))
    if len(rows) != generation['part_count'] or [row['position'] for row in rows] != list(range(len(rows))):
        raise MigrationError('Prepared action original has incomplete parts')
    text = ''.join(files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                             chunk_key='original:' + str(row['position'])) for row in rows)
    raw = text.encode()
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared action differs from its captured bytes')
    return json.loads(text)


DEFINITION = Job('action_ledger', 79, 97, {'actions': ('id',)}, tuple(definitions()), None, None,
    dependencies=('identities','playlist_scopes','playlist_membership_refs','recordings'), refresh_highwater=False,
    # Actions also reference other actions. An insertion/deletion can resolve
    # or withdraw an older undo link without editing that older source row.
    optional_dependencies=('actions',),
    projections={'actions': ('CAST(id AS TEXT) AS source','id AS action_id','kind','status','created_at','executed_at', *(
        f"CASE WHEN {field} IS NULL THEN NULL ELSE COALESCE(substr(CAST({field} AS BLOB),1,{MAX_SOURCE_BYTES+1}),X'') END AS {field}"
        for field in ('params_json','plan_json','undo_json')))})
JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete, revision_aware=True, resume_read=resume_read)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
