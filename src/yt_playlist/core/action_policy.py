"""Finite action journals and the states derived from their captured progress."""
import json
from pathlib import PurePath

from yt_playlist.util.action_kinds import is_undoable

OPERATIONS = {'backup': 'restore_backup', 'keeper': 'restore_keeper', 'copy': 'delete_copy'}
CLAIM_SECONDS = 60
FINISHED = ('undone', 'cancelled')


def decode(raw, *, draft=False):
    if raw is None:
        return None
    if draft:
        if not isinstance(raw, str):
            raise ValueError('A merge draft signature requires text')
        return raw
    value = json.loads(raw)
    if not isinstance(value, (dict, list)):
        raise ValueError('Unsupported action document')
    return value


def require_undoable(action):
    if action is None:
        raise ValueError('No such action')
    if not is_undoable(action.kind) or action.status not in ('executed', 'delete_failed', 'undo_failed', 'undoing'):
        raise ValueError(f'action {action.id} ({action.kind}/{action.status}) is not undoable')


def imported_pending(action, step):
    return (action['idempotency_key'] == 'legacy-action:' + str(action['id'])
            and step['operation'] == 'delete_playlist' and step['phase'] == 'pending'
            and step['outcome'] == 'not_sent')


def step_state(action, operation, value, *, legacy=False):
    phase = value.get('phase', 'pending')
    if legacy and phase == 'pending' and operation == 'delete_playlist' or imported_pending(
            action, {'operation': operation, 'phase': phase, 'outcome': 'not_sent'}):
        phase = 'uncertain'
    outcome = ('unknown' if phase in ('uncertain', 'creating', 'deleting') or
        phase == 'restoring' and value.get('observed') is None else
        'complete' if phase == 'complete' else
        'confirmed_absent' if phase == 'remote_complete' and operation in ('delete_playlist', 'delete_copy') else
        'confirmed_present' if phase in ('failed', 'remote_complete', 'restoring') else 'not_sent')
    observed = 'unrecorded' if 'observed' not in value else 'unknown' if value['observed'] is None else 'present'
    return phase, outcome, observed


def progress(kind, params, plan, undo, status):
    """Describe targets and recovery steps without reading storage or files."""
    params = params if isinstance(params, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    undo = undo if isinstance(undo, dict) else {}
    backups = list(undo.get('backups', []))
    backups.extend(entry['backup'] for entry in undo.get('restored', []) if entry.get('backup'))
    if undo.get('backup'):
        backups.append(undo['backup'])
    backups.extend(entry['backup'] for entry in undo.get('deletions', []) if entry.get('backup'))
    backups.extend(entry.get('backup') or entry['deletion_backup'] for entry in
        (undo.get('recovery') or {}).get('steps', []) if entry.get('backup') or entry.get('deletion_backup'))
    targets, steps = [], []
    for role, field in (('source', 'source'), ('destination', 'target')):
        if type(params.get(field)) is int:
            targets.append((role, 0, dict(membership=params[field],
                remote=params.get('source_ytm') if role == 'source' else None)))
    placement = None
    if type(plan.get('playlist_id')) is int:
        targets.append(('placement', 0, dict(membership=plan['playlist_id'])))
        videos, handles = plan.get('video_ids', []), plan.get('set_video_ids', {})
        if (not isinstance(videos, list) or any(not isinstance(v, str) or not v for v in videos)
                or not isinstance(handles, dict) or any(not isinstance(v, str) or not v or not isinstance(h, str) or not h
                                                       for v, h in handles.items())):
            raise ValueError('Unsupported ordered action placement')
        placement = (videos, handles, plan.get('after_video_id'), 'set_video_ids' in plan)
        phase = 'complete' if status == 'executed' else 'cancelled' if status == 'cancelled' else 'pending'
        steps.append((750000, 'place_tracks', {'phase': phase}, ('placement', 0), None))
    resolutions = []
    if kind == 'plan':
        for list_kind in ('additions', 'unresolved'):
            for position, item in enumerate(plan.get(list_kind, [])):
                if not isinstance(item, dict) or set(item) != {'identity_key', 'source_video_id', 'target_video_id', 'method'}:
                    raise ValueError('Unsupported merge plan resolution')
                resolutions.append((list_kind, position, item))
    for position, entry in enumerate(undo.get('restored', [])):
        targets.append(('keeper', position, dict(account=entry.get('identity'), remote=entry.get('ytm'))))
        if entry.get('backup'):
            steps.append((500000 + position, 'merge_plan', entry, ('keeper', position), entry['backup']))
    if undo.get('new_ytm') is not None:
        targets.append(('destination', 0, dict(account=undo.get('target_identity'), remote=undo['new_ytm'])))
    for position, entry in enumerate(undo.get('deletions', [])):
        targets.append(('deletion', position, dict(account=entry.get('identity'), remote=entry.get('ytm'))))
        steps.append((position, 'delete_playlist', entry, ('deletion', position), entry.get('backup')))
    journal = undo.get('recovery')
    if journal is not None:
        if not isinstance(journal, dict) or journal.get('version') not in (1, 2) or not isinstance(journal.get('steps'), list):
            raise ValueError('Unsupported action undo journal')
        entries = sorted(journal['steps'], key=lambda step: step['kind'] != 'backup') if journal['version'] == 1 else journal['steps']
        for position, entry in enumerate(entries):
            operation = OPERATIONS.get(entry.get('kind'))
            if operation is None:
                raise ValueError('Unsupported action undo operation')
            backup = (entry.get('backup') if operation == 'restore_backup' else
                      entry.get('deletion_backup') if operation == 'delete_copy' else None)
            target = None
            if operation == 'restore_keeper':
                target = dict(account=entry['entry'].get('identity'), remote=entry['entry'].get('ytm'))
            elif operation == 'delete_copy':
                target = dict(account=entry.get('identity'), remote=entry.get('ytm'))
            if target is not None:
                targets.append(('recovery', position, target))
            steps.append((1000000 + position, operation, entry,
                          ('recovery', position) if target is not None else None, backup))
    return dict(backups=list(dict.fromkeys(backups)), targets=targets, steps=steps,
                placement=placement, resolutions=resolutions)


def recovery_entry(undo, step):
    if step['operation'] == 'delete_playlist':
        return undo['deletions'], step['position']
    if step['operation'] == 'merge_plan':
        return undo['restored'], step['position'] - 500000
    return undo['recovery']['steps'], step['position'] - 1000000


def upgrade_journal(undo):
    journal = undo.get('recovery')
    if isinstance(journal, dict) and journal.get('version') == 1:
        journal['steps'].sort(key=lambda step: step['kind'] != 'backup')
        journal['version'] = 2
        return True
    return False


def relocate_backup(backups, filename):
    for row in backups:
        if filename in (row['filename'], row['retained_filename']) and row['retained_filename'] is not None:
            return row['retained_filename']
    path = PurePath(filename)
    if path.suffix == '.json' and len(path.stem) == 64 and all(char in '0123456789abcdef' for char in path.stem):
        for row in backups:
            if row['digest'] == path.stem and row['availability'] == 'verified':
                return row['retained_filename']
    return filename
