"""Coordinate action journals, retained backups, and the action SQL owner."""
import hashlib
import json
from uuid import uuid4

from yt_playlist.controllers.action_documents import ActionDocuments
from yt_playlist.core import action_policy
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.util.action_kinds import COPY_PLAYLIST, UNDO


class Actions:
    def __init__(self, repository, files, backups, database_identity, guard):
        self.repository, self.files, self.backups = repository, files, backups
        self.documents = ActionDocuments(files)
        self.database_identity, self.guard = database_identity, guard

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

    def versions(self, kind):
        return self.repository.versions(kind)

    def get_draft(self, signature):
        return self._one(self.repository.get_draft, signature)

    def get_pending_deletion(self, account_id, provider_playlist_id):
        return self._one(self.repository.deletion_owner, account_id, provider_playlist_id, 'delete_pending')

    def get_failed_deletion(self, account_id, provider_playlist_id):
        return self._one(self.repository.deletion_owner, account_id, provider_playlist_id, 'delete_failed')

    def get_pending_actions(self, kind):
        return self._many(self.repository.pending_actions, kind)

    def get_pending_placements(self, membership_id):
        return self._many(self.repository.pending_placements, membership_id)

    def record_action(self, kind, params_json, plan_json, status, undo_json, created_at, *, idempotency_key=None, reviewed_draft=None):
        with self.guard(), self.files.retained():
            prepared = self._prepare_record(kind, params_json, plan_json, status, undo_json, created_at,
                                            idempotency_key=idempotency_key, reviewed_draft=reviewed_draft)
            return prepared['reuse'] if 'reuse' in prepared else self.repository.publish_record(prepared)

    def _publish(self, kind, owner, value, *, new_owner=False):
        return self._write_reserved(*self._reserve(kind, owner, value, new_owner=new_owner))

    def _reserve(self, kind, owner, value, *, new_owner=False):
        integrity = self.files.describe(kind, owner, value)
        file_id = self.repository.reserve_file(kind, owner, integrity, new_owner=new_owner)
        return file_id, kind, owner, value, integrity

    def _write_reserved(self, file_id, kind, owner, value, integrity):
        reference = self.files.publish_reserved(file_id, kind, owner, value, expected=integrity)
        return file_id, reference

    def reserve_radio_copy(self, params_json, undo_json, now):
        return self._prepare_record(COPY_PLAYLIST, params_json, '{}', 'executed', undo_json, now, reserve=True)

    def write_radio_copy(self, prepared):
        return {**prepared, 'publication': self._write_reserved(*prepared['publication'])}

    def _prepare_record(self, kind, params_json, plan_json, status, undo_json, created_at, *,
                        idempotency_key=None, reviewed_draft=None, reserve=False):
        draft = kind == 'merge_draft'
        params, plan, undo = (action_policy.decode(params_json), action_policy.decode(plan_json, draft=draft),
                              action_policy.decode(undo_json, draft=draft))
        if draft and (plan is None or status != 'draft'):
            raise ValueError('A merge draft requires a signature and draft status')
        intent = {'kind': kind, 'params': params, 'plan': plan}
        if reviewed_draft is not None:
            intent['reviewed_draft'] = list(reviewed_draft)
        digest = hashlib.sha256(canonical(intent)).hexdigest()
        if idempotency_key is not None:
            prior = self.repository.prior_intent(idempotency_key)
            if prior:
                if prior['intent_digest'] != digest:
                    raise ValueError('Idempotency key belongs to another action intent')
                return dict(reuse=prior['id'], idempotency_key=idempotency_key, intent_digest=digest)
        identity, revision = self.repository.next_action_id(), uuid4().hex
        captured = {'params': params_json, 'plan': plan_json, 'undo': undo_json}
        reviewed = None
        if reviewed_draft is not None:
            reviewed = self.repository.reviewed_draft(*reviewed_draft)
            selection = self.documents.draft(reviewed)
            captured['reviewed_draft'] = {'id': reviewed['id'], 'revision': reviewed['revision'],
                'signature': reviewed['draft_signature'],
                'params': selection['params']}
        undo_of = params.get('undid') if kind == UNDO and isinstance(params, dict) else None
        if undo_of is not None and self.repository.header(undo_of) is None:
            raise ValueError('Undo history refers to a missing action')
        # Validate the full progress shape before reserving or writing any body.
        progress = None if draft else action_policy.progress(kind, params, plan, undo, status)
        key = idempotency_key or 'action:' + uuid4().hex
        publish = self._reserve if reserve else self._publish
        publication = publish('action-draft' if draft else 'action', identity,
                              {'params': params_json, 'undo': undo_json} if draft else captured, new_owner=True)
        header = dict(id=identity, idempotency_key=key)
        return dict(identity=identity, revision=revision, kind=kind, status=status, created_at=created_at,
            idempotency_key=key, intent_digest=digest, undo_of=undo_of,
            mode=params.get('mode') if kind == 'plan' and isinstance(params, dict) else None,
            publication=publication, reviewed=reviewed, signature=plan if draft else None,
            progress=(progress if reserve else None if draft else self._prepare_progress(identity, progress, header, None)))

    def _prepare_progress(self, identity, prepared, header, now):
        backups = self.repository.backups(identity)
        previous_backups = [dict(row) for row in backups]
        added_backups, steps = [], []
        position = max((row['position'] for row in backups), default=-1) + 1
        for filename in prepared['backups']:
            if any(filename in (row['filename'], row['retained_filename']) for row in backups):
                continue
            reference, _ = self.backups.reference(filename)
            retained = None
            if reference[3] == 'verified':
                retained = self.backups.retain(filename, expected=reference[1:3])
            added_backups.append((position, reference, retained))
            backups.append(dict(position=position, filename=filename, retained_filename=retained,
                                digest=reference[1], bytes=reference[2], availability=reference[3]))
            position += 1
        targets = list(prepared['targets'])
        for position, operation, value, target_key, filename in prepared['steps']:
            filename = action_policy.relocate_backup(backups, filename) if filename is not None else None
            step, prior = self.repository.step_input(identity, position)
            historical = bool(prior and prior['backup_status'] == 'missing_historical' and prior['backup_filename'] == filename)
            reference, captured = self.backups.reference(filename, historical=historical)
            if operation == 'restore_backup' and captured and captured.get('ytm_playlist_id') is not None:
                target_key = 'recovery', position - 1000000
                targets.append((*target_key, dict(account=captured.get('identity_id'), remote=captured['ytm_playlist_id'])))
            evidence = self._publish('action-step', step, value, new_owner=prior is None)
            steps.append(dict(id=step, prior=prior, position=position, operation=operation, value=value,
                              target=target_key, backup=reference, publication=evidence,
                              state=action_policy.step_state(header, operation, value)))
        return dict(previous_backups=previous_backups, backups=added_backups, targets=targets, steps=steps, now=now)

    def update_action(self, action_id, status, executed_at, undo_json=None, *, params_json=None, expected_revision=None, target_claims=()):
        with self.guard(), self.files.retained():
            prepared = self._prepare_update(action_id, status, executed_at, undo_json, params_json,
                                            expected_revision, target_claims)
            self.repository.publish_update(prepared)

    def _prepare_update(self, action_id, status, executed_at, undo_json, params_json=None, expected_revision=None, target_claims=()):
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
        progress = action_policy.progress(row['kind'], params, plan, undo, status)
        return dict(identity=action_id, status=status, executed_at=executed_at,
            previous_revision=row['revision'], revision=uuid4().hex, target_claims=tuple(target_claims),
            progress=self._prepare_progress(action_id, progress, row, executed_at),
            publication=self._publish('action', action_id, value))

    def update_draft(self, action_id, params_json):
        with self.guard(), self.files.retained():
            action_policy.decode(params_json)
            row = self.repository.header(action_id)
            if row is None or row['status'] != 'draft' or row['kind'] != 'merge_draft':
                raise ValueError('Merge draft is no longer editable')
            draft = self.documents.draft(row)
            draft['params'] = params_json
            publication = self._publish('action-draft', action_id, draft)
            self.repository.publish_draft(action_id, row['revision'], uuid4().hex, publication)

    def delete_action(self, action_id):
        self.repository.delete_action(action_id)

    def claim_undo(self, action_id, now):
        with self.guard(), self.files.retained():
            action = self.get_action(action_id)
            action_policy.require_undoable(action)
            with self.repository.transaction():
                self.repository.claim_execution(action_id, 'undoing', now,
                    previous_revision=action.revision, token=uuid4().hex, revision=uuid4().hex)
            return self.get_action(action_id)

    def _require_targets(self, claims):
        for target, token in claims:
            self.repository.require_claim(target, token)

    def save_undo_progress(self, action_id, undo, now, *, failed=False, claim_token=None, target_claims=()):
        with self.guard(), self.files.retained():
            status = 'undo_failed' if failed else 'undoing'
            self.repository.require_execution(action_id, claim_token)
            prepared = self._prepare_update(action_id, status, now, json.dumps(undo), target_claims=target_claims)
            self.repository.publish_update(prepared, execution_token=claim_token, require_execution=True)

    def finish_undo(self, action_id, undo, now, *, claim_token=None, target_claims=()):
        with self.guard(), self.files.retained():
            params, key = json.dumps({'undid': action_id}), 'undo:' + str(action_id)
            self.repository.require_execution(action_id, claim_token)
            prepared = self._prepare_update(action_id, 'undone', now, json.dumps(undo), target_claims=target_claims)
            history = self._prepare_record(UNDO, params, '{}', 'executed', '{}', now, idempotency_key=key)
            self.repository.finish_undo(prepared, history, claim_token)

    def _claim_target(self, identity, now, *, reconcile=False):
        target, prior, uncertain = self.repository.claim_facts(identity, action_policy.FINISHED)
        if target is None or target['account_id'] is None or target['playlist_scope_id'] is None:
            raise ValueError('Reconnect and resolve this historical action target before recovery')
        if prior and prior['claim_until'] > now:
            raise ValueError('Another action worker owns this playlist target')
        if uncertain is not None and (not reconcile or uncertain != target['action_id']):
            raise ValueError('The prior remote outcome is unknown; reconcile it before another write')
        token = uuid4().hex
        self.repository.claim_target(identity, token, now + action_policy.CLAIM_SECONDS,
                                     previous=prior['id'] if prior else None)
        return token

    def claim_undo_targets(self, action_id, execution_token, now):
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
        return self.repository.target_lock_keys(action_id, role)

    def require_known_remote_outcome(self, account, remote):
        if self.repository.uncertain_owner(account, remote, action_policy.FINISHED) is not None:
            raise ValueError('The prior remote outcome is unknown; reconcile it before another write')

    def claim_forward_targets(self, action_id, role, now):
        with self.guard(), self.repository.transaction():
            return [(target['id'], self._claim_target(target['id'], now)) for target in self.repository.targets(action_id, role)]

    def begin_deletion(self, action_id, account, remote, now, *, reconcile=False):
        with self.guard(), self.files.retained():
            step = self.repository.deletion_step(action_id, account, remote)
            if step is None:
                raise ValueError('Deletion has no committed account target and recovery step')
            if not reconcile:
                if step['phase'] != 'pending' or step['outcome'] != 'not_sent' or step['backup_status'] != 'verified':
                    raise ValueError('Deletion requires its verified backup and an unsent intent')
                reference, _ = self.backups.reference(step['backup_filename'])
                if reference[:3] != (step['backup_filename'], step['backup_digest'], step['backup_bytes']):
                    raise ValueError('Deletion backup changed after the intent was committed')
            with self.repository.transaction():
                if self.repository.deletion_step(action_id, account, remote) != step:
                    raise ValueError('Deletion intent changed before it was claimed')
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
