"""Coordinate action journals, retained backups, and the action SQL owner."""
import hashlib
import json
from uuid import uuid4

from yt_playlist.controllers.action_documents import ActionDocuments
from yt_playlist.core import action_policy
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.util.action_kinds import UNDO


class Actions:
    def __init__(self, repository, files, backups, database_identity, guard):
        self.repository, self.files, self.backups = repository, files, backups
        self.documents = ActionDocuments(files)
        self.database_identity, self.guard = database_identity, guard

    def has_durable_targets(self):
        return self.repository.has_durable_targets()

    def undo_lock_key(self, action_id):
        return hashlib.sha256(f'{self.database_identity}:{action_id}'.encode()).hexdigest()

    def _one(self, method, *args):
        with self.guard(), self.files.retained():
            return self.documents.action(method(*args))

    def _many(self, method, *args):
        with self.guard(), self.files.retained():
            return [self.documents.action(facts) for facts in method(*args)]

    def get_action(self, action_id):
        return self._one(self.repository.get_action, action_id)

    def get_actions(self):
        return self._many(self.repository.get_actions)

    def get_draft(self, signature):
        return self._one(self.repository.get_draft, signature)

    def get_pending_deletion(self, account_id, provider_playlist_id):
        return self._one(self.repository.deletion_owner, account_id, provider_playlist_id, 'delete_pending')

    def get_failed_deletion(self, account_id, provider_playlist_id):
        return self._one(self.repository.deletion_owner, account_id, provider_playlist_id, 'delete_failed')

    def get_pending_actions(self, kind):
        return self._many(self.repository.pending_actions, kind)

    def get_pending_placements(self, membership_id):
        if self.has_durable_targets():
            return self._many(self.repository.pending_placements, membership_id)
        return [action for action in self.get_pending_actions('add_tracks')
                if json.loads(action.plan_json).get('playlist_id') == membership_id]

    def record_action(self, kind, params_json, plan_json, status, undo_json, created_at, *, idempotency_key=None, reviewed_draft=None):
        with self.guard(), self.files.retained(), self.repository.transaction():
            if not self.has_durable_targets():
                return self.repository.insert_action(kind, params_json, plan_json, status, undo_json, created_at)
            draft = kind == 'merge_draft'
            params, plan, undo = (action_policy.decode(params_json), action_policy.decode(plan_json, draft=draft),
                                  action_policy.decode(undo_json, draft=draft))
            intent = {'kind': kind, 'params': params, 'plan': plan}
            if reviewed_draft is not None:
                intent['reviewed_draft'] = list(reviewed_draft)
            digest = hashlib.sha256(canonical(intent)).hexdigest()
            if idempotency_key is not None:
                prior = self.repository.prior_intent(idempotency_key)
                if prior:
                    if prior['intent_digest'] != digest:
                        raise ValueError('Idempotency key belongs to another action intent')
                    return prior['id']
            identity, revision = self.repository.next_action_id(), uuid4().hex
            captured = {'params': params_json, 'plan': plan_json, 'undo': undo_json}
            reviewed = None
            if reviewed_draft is not None:
                reviewed = self.repository.reviewed_draft(*reviewed_draft)
                captured['reviewed_draft'] = {'id': reviewed['id'], 'revision': reviewed['revision'],
                    'signature': reviewed['signature'],
                    'params': self.documents.read(reviewed, 'action-draft', reviewed['id'])}
            undo_of = params.get('undid') if kind == UNDO and isinstance(params, dict) else None
            if undo_of is not None and self.repository.header(undo_of) is None:
                raise ValueError('Undo history refers to a missing action')
            reference = self.files.publish('action', identity, captured)
            self.repository.insert_action(kind, params_json, plan_json, status, undo_json, created_at,
                identity=identity, revision=revision, idempotency_key=idempotency_key or 'action:'+uuid4().hex,
                intent_digest=digest, undo_of=undo_of, mode=params.get('mode') if kind == 'plan' and isinstance(params, dict) else None,
                reference=reference, reviewed=reviewed)
            if draft:
                evidence = self.files.publish('action-draft', identity, params_json)
                self.repository.store_draft(identity, plan, revision, created_at, evidence)
            else:
                self._progress(identity, kind, params, plan, undo, None, status)
            return identity

    def _progress(self, identity, kind, params, plan, undo, now, status):
        prepared = action_policy.progress(kind, params, plan, undo, status)
        backups = self.repository.backups(identity)
        position = max((row['position'] for row in backups), default=-1) + 1
        for filename in prepared['backups']:
            if any(filename in (row['filename'], row['retained_filename']) for row in backups):
                continue
            reference, _ = self.backups.reference(filename)
            retained = self.backups.retain(filename)[0] if reference[3] == 'verified' else None
            self.repository.store_backup(identity, position, reference, retained)
            backups.append(dict(position=position, filename=filename, retained_filename=retained,
                                digest=reference[1], bytes=reference[2], availability=reference[3]))
            position += 1
        targets = {}
        for role, position, request in prepared['targets']:
            targets[role, position] = self.repository.store_target(identity, role, position, **request)
        if prepared['placement'] is not None:
            self.repository.store_placement(identity, targets['placement', 0], *prepared['placement'])
        self.repository.store_resolutions(identity, prepared['resolutions'])
        header = self.repository.header(identity)
        for position, operation, value, target_key, filename in prepared['steps']:
            target = targets[target_key] if target_key is not None else None
            filename = action_policy.relocate_backup(backups, filename) if filename is not None else None
            step, prior = self.repository.step_input(identity, position)
            historical = bool(prior and prior['backup_status'] == 'missing_historical' and prior['backup_filename'] == filename)
            reference, captured = self.backups.reference(filename, historical=historical)
            if operation == 'restore_backup' and captured and captured.get('ytm_playlist_id') is not None:
                target = self.repository.store_target(identity, 'recovery', position - 1000000,
                    account=captured.get('identity_id'), remote=captured['ytm_playlist_id'])
            if prior and (prior['operation'] != operation or prior['target_id'] != target):
                raise ValueError('A committed recovery step cannot change its operation or target')
            evidence = self.files.publish('action-step', step, value)
            phase, outcome, observed = action_policy.step_state(header, operation, value)
            self.repository.store_step(step, identity, target, position, operation, value, phase, outcome, observed,
                                       reference, evidence, now)

    def update_action(self, action_id, status, executed_at, undo_json=None, *, params_json=None, expected_revision=None, target_claims=()):
        with self.guard(), self.files.retained(), self.repository.transaction():
            if not self.has_durable_targets():
                self.repository.update_action(action_id, status, executed_at, undo=undo_json, params=params_json)
                return
            self._require_targets(target_claims)
            facts = self.repository.get_action(action_id)
            if facts is None:
                raise ValueError('Action no longer exists')
            row = facts['row']
            if expected_revision is not None and row['revision'] != expected_revision:
                raise ValueError('Action was updated by another worker')
            value = self.documents.document(facts)
            if undo_json is not None:
                value['undo'] = undo_json
            if params_json is not None:
                value['params'] = params_json
            params, plan, undo = (action_policy.decode(value[field]) for field in ('params', 'plan', 'undo'))
            self._progress(action_id, row['kind'], params, plan, undo, executed_at, status)
            reference = self.files.publish('action', action_id, value)
            self.repository.update_action(action_id, status, executed_at, revision=uuid4().hex, reference=reference)

    def update_draft(self, action_id, params_json):
        with self.guard(), self.files.retained(), self.repository.transaction():
            if not self.has_durable_targets():
                self.repository.update_draft(action_id, params_json, None, None, None)
                return
            action_policy.decode(params_json)
            row = self.repository.header(action_id)
            if row is None or row['status'] != 'draft' or row['kind'] != 'merge_draft':
                raise ValueError('Merge draft is no longer editable')
            reference = self.files.publish('action-draft', action_id, params_json)
            self.repository.update_draft(action_id, params_json, uuid4().hex, None, reference)

    def delete_action(self, action_id):
        self.repository.delete_action(action_id)

    def claim_undo(self, action_id, now):
        with self.guard(), self.files.retained(), self.repository.transaction():
            action = self.get_action(action_id)
            action_policy.require_undoable(action)
            self.repository.claim_execution(action_id, 'undoing', now, previous_status=action.status,
                previous_revision=action.revision, token=uuid4().hex, revision=uuid4().hex)
            return self.get_action(action_id) if self.has_durable_targets() else action

    def _require_targets(self, claims):
        for target, token in claims:
            self.repository.require_claim(target, token)

    def _require_undo(self, action_id, token):
        if self.has_durable_targets():
            self.repository.require_execution(action_id, token)
        else:
            row = self.repository.header(action_id)
            if row is None or row['status'] != 'undoing':
                raise ValueError('Undo no longer owns this action. Reload the Action Log.')

    def save_undo_progress(self, action_id, undo, now, *, failed=False, claim_token=None, target_claims=()):
        with self.guard(), self.files.retained(), self.repository.transaction():
            self._require_undo(action_id, claim_token)
            self._require_targets(target_claims)
            self.update_action(action_id, 'undo_failed' if failed else 'undoing', now, undo_json=json.dumps(undo))

    def finish_undo(self, action_id, undo, now, *, claim_token=None, target_claims=()):
        with self.guard(), self.files.retained(), self.repository.transaction():
            self._require_undo(action_id, claim_token)
            self._require_targets(target_claims)
            self.update_action(action_id, 'undone', now, undo_json=json.dumps(undo))
            if self.has_durable_targets():
                self.repository.clear_execution(action_id)
            self.record_action(UNDO, json.dumps({'undid': action_id}), '{}', 'executed', '{}', now,
                               idempotency_key='undo:'+str(action_id))

    def _claim_target(self, identity, now, *, reconcile=False):
        target, prior, uncertain = self.repository.claim_facts(identity, action_policy.FINISHED)
        if target is None or target['account_id'] is None or target['playlist_scope_id'] is None:
            raise ValueError('Reconnect and resolve this historical action target before recovery')
        if prior and prior['lease_until'] > now:
            raise ValueError('Another action worker owns this playlist target')
        if uncertain is not None and (not reconcile or uncertain != target['action_id']):
            raise ValueError('The prior remote outcome is unknown; reconcile it before another write')
        token = uuid4().hex
        self.repository.claim_target(identity, target['account_id'], target['playlist_scope_id'], token, now + action_policy.CLAIM_SECONDS)
        return token

    def claim_undo_targets(self, action_id, execution_token, now):
        if not self.has_durable_targets():
            return []
        with self.guard(), self.repository.transaction():
            self.repository.require_execution(action_id, execution_token)
            claims, scopes = [], set()
            for target in self.repository.targets(action_id, 'recovery', incomplete=True):
                scope = target['account_id'], target['playlist_scope_id']
                if scope not in scopes:
                    scopes.add(scope)
                    claims.append((target['id'], self._claim_target(target['id'], now, reconcile=True)))
            return claims

    def target_lock_keys(self, action_id, role):
        return self.repository.target_lock_keys(action_id, role) if self.has_durable_targets() else []

    def require_known_remote_outcome(self, account, remote):
        if self.repository.uncertain_owner(account, remote, action_policy.FINISHED) is not None:
            raise ValueError('The prior remote outcome is unknown; reconcile it before another write')

    def claim_forward_targets(self, action_id, role, now):
        if not self.has_durable_targets():
            return []
        with self.guard(), self.repository.transaction():
            return [(target['id'], self._claim_target(target['id'], now)) for target in self.repository.targets(action_id, role)]

    def begin_deletion(self, action_id, account, remote, now, *, reconcile=False):
        if not self.has_durable_targets():
            return None
        with self.guard(), self.files.retained(), self.repository.transaction():
            step = self.repository.deletion_step(action_id, account, remote)
            if step is None:
                raise ValueError('Deletion has no committed account target and recovery step')
            if not reconcile:
                if step['phase'] != 'pending' or step['outcome'] != 'not_sent' or step['backup_status'] != 'verified':
                    raise ValueError('Deletion requires its verified backup and an unsent intent')
                reference, _ = self.backups.reference(step['backup_filename'])
                if reference[:3] != (step['backup_filename'], step['backup_digest'], step['backup_bytes']):
                    raise ValueError('Deletion backup changed after the intent was committed')
            token = self._claim_target(step['target_id'], now, reconcile=reconcile)
            if not reconcile:
                self.repository.mark_step(step['id'], 'uncertain', 'unknown')
            return step['target_id'], token

    def release_deletion(self, claim):
        if claim is not None:
            self.repository.release_deletion(claim)

    def save_deletion_progress(self, action_id, undo, now, claims):
        from yt_playlist.library.playlist_deletion import deletion_status
        self.update_action(action_id, deletion_status(undo), now, undo_json=json.dumps(undo),
                           target_claims=tuple(claims.values()))
