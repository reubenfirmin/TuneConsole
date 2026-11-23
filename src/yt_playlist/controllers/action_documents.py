"""Compose action documents from detached SQL facts and protected recovery files."""
import json

from yt_playlist.core import action_policy
from yt_playlist.repos.models import Action


class ActionDocuments:
    def __init__(self, files):
        self.files = files

    def read(self, row, kind, owner):
        reference = tuple(row[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
        return self.files.read(reference, kind=kind, owner=owner)

    def action(self, facts):
        if facts is None:
            return None
        row = facts['row']
        value = self.document(facts)
        return Action(row['id'], row['kind'],
            value['params'], value['plan'], value['undo'], row['status'], row['created_at'], row['executed_at'],
            row['revision'], row['execution_token'])

    def draft(self, row):
        value = self.read(row, 'action-draft', row['id'])
        # Earlier draft documents contained only the selected parameters.
        return value if isinstance(value, dict) else dict(params=value, undo=None)

    def document(self, facts):
        row = facts['row']
        if row['kind'] == 'merge_draft':
            return dict(self.draft(row), plan=row['draft_signature'])
        value = self.read(row, 'action', row['id'])
        params, plan = action_policy.decode(value['params']), action_policy.decode(value['plan'])
        targets = {target['id']: target for target in facts['targets']}
        slots = {(target['role'], target['position']): target for target in targets.values()}
        if row['kind'] == 'plan' and isinstance(params, dict) and isinstance(plan, dict):
            params['mode'] = row['execution_mode']
            for role, field in (('source', 'source'), ('destination', 'target')):
                target = slots.get((role, 0))
                if target:
                    params[field] = target['membership_ref_id'] if target['membership_ref_id'] is not None else target['unresolved_membership_ref_id']
                    if role == 'source':
                        params['source_ytm'] = target['provider_playlist_id_snapshot']
            value['params'] = json.dumps(params)
        target = slots.get(('placement', 0))
        if target is not None and isinstance(plan, dict):
            plan['playlist_id'] = target['membership_ref_id'] if target['membership_ref_id'] is not None else target['unresolved_membership_ref_id']
            plan.setdefault('after_video_id', None)
            plan.setdefault('video_ids', [])
            value['plan'] = json.dumps(plan)
        undo = action_policy.decode(value['undo'])
        if not isinstance(undo, dict):
            return value
        changed = action_policy.upgrade_journal(undo)
        if undo.get('backup'):
            undo['backup'] = action_policy.relocate_backup(facts['backups'], undo['backup'])
            changed = True
        if 'backups' in undo:
            undo['backups'] = [action_policy.relocate_backup(facts['backups'], filename) for filename in undo['backups']]
            changed = True
        for step in facts['steps']:
            if step['operation'] == 'place_tracks':
                continue
            entry = self.read(step, 'action-step', step['id'])
            entry['phase'] = 'uncertain' if action_policy.imported_pending(row, step) else step['phase']
            if step['backup_filename'] is not None:
                field = 'deletion_backup' if step['operation'] == 'delete_copy' else 'backup'
                entry[field] = action_policy.relocate_backup(facts['backups'], step['backup_filename'])
            for column, field in (('creation_marker', 'marker'), ('created_provider_id_snapshot', 'created_id'),
                                  ('original_survived', 'original_survived')):
                if step[column] is not None:
                    entry[field] = bool(step[column]) if field == 'original_survived' else step[column]
            if step['observed_status'] == 'unknown':
                entry['observed'] = None
            if step['operation'] == 'delete_playlist':
                target = targets[step['target_id']]
                entry['identity'] = target['account_id'] if target['account_id'] is not None else target['unresolved_account_id']
                entry['ytm'] = target['provider_playlist_id_snapshot']
            entries, index = action_policy.recovery_entry(undo, step)
            entries[index] = entry
            changed = True
        if changed:
            value['undo'] = json.dumps(undo)
        return value
