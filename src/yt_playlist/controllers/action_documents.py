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
        row, layout = facts['row'], facts['layout']
        value = self.document(facts)
        return Action(row['id'] if layout != 'prepared' else row['action_id'], row['kind'],
            value['params'], value['plan'], value['undo'], row['status'], row['created_at'], row['executed_at'],
            row['revision'] if layout == 'owned' else None, row['execution_token'] if layout == 'owned' else None)

    def document(self, facts):
        row, layout = facts['row'], facts['layout']
        if layout == 'legacy':
            value = {field: row[field+'_json'] for field in ('params', 'plan', 'undo')}
            if facts['prepared'] is not None:
                captured = self.read(facts['prepared']['row'], 'action', facts['prepared']['row']['source'])
                if captured['undo'] == value['undo']:
                    value['undo'] = self._prepared(facts['prepared'], captured)['undo']
            return value
        value = self.read(row, 'action', row['id'] if layout == 'owned' else row['source'])
        return self._owned(facts, value) if layout == 'owned' else self._prepared(facts, value)

    def _prepared(self, facts, value):
        if facts['row']['kind'] == 'merge_draft' or value['undo'] is None:
            return value
        undo = json.loads(value['undo'])
        if not isinstance(undo, dict):
            return value
        backups = {row['backup_filename']: row['retained_filename'] for row in facts['backups']
                   if row['backup_status'] == 'verified'}
        changed = action_policy.upgrade_journal(undo)
        if undo.get('backup') in backups:
            undo['backup'] = backups[undo['backup']]
            changed = True
        if 'backups' in undo and any(filename in backups for filename in undo['backups']):
            undo['backups'] = [backups.get(filename, filename) for filename in undo['backups']]
            changed = True
        for step in facts['steps']:
            if step['operation'] == 'place_tracks':
                continue
            entries, index = action_policy.recovery_entry(undo, step)
            entry = entries[index]
            if entry.get('phase') != step['phase']:
                entry['phase'] = step['phase']
                changed = True
            for field in ('backup', 'deletion_backup'):
                if entry.get(field) in backups:
                    entry[field] = backups[entry[field]]
                    changed = True
        if changed:
            value['undo'] = json.dumps(undo)
        return value

    def _owned(self, facts, value):
        row = facts['row']
        if row['kind'] == 'merge_draft':
            value['plan'] = facts['draft']['signature']
            value['params'] = self.read(facts['draft'], 'action-draft', row['id'])
            return value
        params, plan = action_policy.decode(value['params']), action_policy.decode(value['plan'])
        targets = {target['id']: target for target in facts['targets']}
        if row['kind'] == 'plan' and isinstance(params, dict) and isinstance(plan, dict):
            params['mode'] = row['execution_mode']
            for role, field in (('source', 'source'), ('destination', 'target')):
                target = next((target for target in targets.values() if target['role'] == role and target['position'] == 0), None)
                if target:
                    params[field] = target['membership_ref_id'] if target['membership_ref_id'] is not None else target['unresolved_membership_ref_id']
                    if role == 'source':
                        params['source_ytm'] = target['provider_playlist_id_snapshot']
            for list_kind in ('additions', 'unresolved'):
                if list_kind in plan:
                    plan[list_kind] = [dict(identity_key=item['captured_identity_key'], source_video_id=item['source_video_id_snapshot'],
                        target_video_id=item['target_video_id_snapshot'], method=item['method'])
                        for item in facts['resolutions'] if item['list_kind'] == list_kind]
            value['params'], value['plan'] = json.dumps(params), json.dumps(plan)
        placement = facts['placement']
        if placement is not None and isinstance(plan, dict):
            target = targets[placement['target_id']]
            plan['playlist_id'] = target['membership_ref_id'] if target['membership_ref_id'] is not None else target['unresolved_membership_ref_id']
            plan['after_video_id'] = placement['after_video_id_snapshot']
            plan['video_ids'] = [item['video_id_snapshot'] for item in facts['items']]
            if placement['handles_present']:
                plan['set_video_ids'] = {item['video_id_snapshot']: item['occurrence_handle_snapshot'] for item in facts['handles']}
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
