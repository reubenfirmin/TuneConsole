"""Action rows, detached recovery references, and conditional SQL claims."""
from contextlib import contextmanager
import json

from yt_playlist.core import action_backup_inventory, action_work, playlist_scopes
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import collection_file_writes


class ActionRepo(Repo):
    @synchronized
    def reserve_file(self, kind, owner, integrity, *, new_owner=False):
        with self.conn.atomic(join=True):
            if new_owner:
                table = 'action_steps' if kind == 'action-step' else 'actions'
                if owner != self._next_identity(table):
                    raise ValueError('Action identity was reserved by another worker')
            return collection_file_writes.reserve(self.conn, kind, owner, integrity,
                                                   identity=owner if new_owner else None)

    def _complete_file(self, kind, owner, publication):
        identity, reference = publication
        collection_file_writes.complete(self.conn, identity, kind, owner, reference)
        return reference

    @synchronized
    def publish_record(self, prepared):
        with self.conn.atomic():
            return self._publish_record(prepared)

    @synchronized
    def publish_radio_copy(self, prepared, *, scope, account):
        return self._publish_record(prepared, copy_destination=(scope, account))

    def _publish_record(self, prepared, *, copy_destination=None):
        prior = self.prior_intent(prepared['idempotency_key']) if copy_destination is None else None
        if prior is not None:
            if prior['intent_digest'] != prepared['intent_digest']:
                raise ValueError('Idempotency key belongs to another action intent')
            return prior['id']
        if 'reuse' in prepared:
            raise ValueError('The reused action no longer exists')
        reviewed = prepared['reviewed']
        if reviewed is not None:
            self.reviewed_draft(reviewed['id'], reviewed['revision'])
        identity = prepared['identity']
        draft = prepared['kind'] == 'merge_draft'
        reference = self._complete_file('action-draft' if draft else 'action', identity, prepared['publication'])
        self.insert_action(prepared['kind'], prepared['status'], prepared['created_at'],
            identity=identity, revision=prepared['revision'], idempotency_key=prepared['idempotency_key'],
            intent_digest=prepared['intent_digest'], undo_of=prepared['undo_of'], mode=prepared['mode'],
            reference=reference, reviewed=reviewed, signature=prepared['signature'])
        if draft:
            return identity
        if copy_destination is None:
            self._store_progress(identity, prepared['progress'])
        else:
            scope, account = copy_destination
            targets = prepared['progress']['targets']
            if targets != [('destination', 0, {'account': account, 'remote': scope})]:
                raise ValueError('Radio Undo does not match the confirmed destination')
            self.conn.execute(
                'INSERT INTO action_targets(action_id,role,position,account_id,playlist_scope_id,'
                "provider_playlist_id_snapshot) VALUES (?,'destination',0,?,?,?)",
                (identity, account, scope, scope))
        return identity

    @synchronized
    def publish_update(self, prepared, *, execution_token=None, require_execution=False):
        with self.conn.atomic():
            self._publish_update(prepared, execution_token=execution_token, require_execution=require_execution)

    def _publish_update(self, prepared, *, execution_token=None, require_execution=False):
        identity = prepared['identity']
        if require_execution:
            self.require_execution(identity, execution_token)
        for target, token in prepared['target_claims']:
            self.require_claim(target, token)
        header = self.header(identity)
        if header is None or header['revision'] != prepared['previous_revision']:
            raise ValueError('Action was updated by another worker')
        self._store_progress(identity, prepared['progress'])
        reference = self._complete_file('action', identity, prepared['publication'])
        self.update_action(identity, prepared['status'], prepared['executed_at'],
                           revision=prepared['revision'], reference=reference)

    @synchronized
    def publish_draft(self, identity, previous_revision, revision, publication):
        with self.conn.atomic():
            row = self.header(identity)
            if (row is None or row['revision'] != previous_revision
                    or row['status'] != 'draft' or row['kind'] != 'merge_draft'):
                raise ValueError('Merge draft is no longer editable')
            reference = self._complete_file('action-draft', identity, publication)
            self.update_draft(identity, revision, None, reference)

    @synchronized
    def finish_undo(self, prepared, history, execution_token):
        with self.conn.atomic():
            self._publish_update(prepared, execution_token=execution_token, require_execution=True)
            self.clear_execution(prepared['identity'])
            self._publish_record(history)

    def _store_progress(self, identity, prepared):
        if self.backups(identity) != prepared['previous_backups']:
            raise ValueError('Action backups changed before progress was committed')
        for position, reference, retained in prepared['backups']:
            self.store_backup(identity, position, reference, retained)
        targets = {(role, position): self.store_target(identity, role, position, **request)
                   for role, position, request in prepared['targets']}
        for step in prepared['steps']:
            prior = self.conn.execute('SELECT * FROM action_steps WHERE action_id=? AND position=?',
                                      (identity, step['position'])).fetchone()
            prior = dict(prior) if prior else None
            if prior != step['prior']:
                raise ValueError('Action recovery step changed before progress was committed')
            if prior is None and self.conn.execute('SELECT 1 FROM action_steps WHERE id=?', (step['id'],)).fetchone():
                raise ValueError('Action recovery identity was used by another worker')
            target = targets[step['target']] if step['target'] is not None else None
            if prior and (prior['operation'] != step['operation'] or prior['target_id'] != target):
                raise ValueError('A committed recovery step cannot change its operation or target')
            reference = self._complete_file('action-step', step['id'], step['publication'])
            self.store_step(step['id'], identity, target, step['position'], step['operation'], step['value'],
                            *step['state'], step['backup'], reference, prepared['now'])

    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            yield

    def _owned_records(self, where='', values=(), *, order='id DESC'):
        conn = self.conn
        borrowed = conn.in_transaction
        if not borrowed:
            conn.execute('BEGIN')
        try:
            rows = conn.execute('SELECT * FROM actions '+where+' ORDER BY '+order, values).fetchall()
            if not rows:
                return []
            records = {}
            for row in rows:
                owner = dict(row)
                backups = action_backup_inventory.decode(row['id'], owner.pop(action_backup_inventory.COLUMN))
                records[row['id']] = dict(row=owner, targets=[], steps=[], backups=backups)
            # Reuse the selected IDs and read the associated rows on the same
            # snapshot, even if another process updates the action's status.
            selection = ' WHERE action_id IN (SELECT value FROM json_each(?))' if where else ''
            ids = (json.dumps(list(records)),) if where else ()
            for table, child_order in (('targets', 'role,position'), ('steps', 'position')):
                for child in conn.execute(f'SELECT * FROM action_{table}{selection} ORDER BY action_id,{child_order}',
                                          ids):
                    if child['action_id'] in records:
                        records[child['action_id']][table].append(dict(child))
            return list(records.values())
        finally:
            if not borrowed:
                conn.rollback()

    @synchronized
    def get_action(self, action_id):
        rows = self._owned_records("WHERE id=? AND kind<>'tv_launch'", (action_id,))
        return rows[0] if rows else None

    @synchronized
    def get_actions(self):
        return self._owned_records("WHERE kind<>'tv_launch'")

    @synchronized
    def versions(self, kind):
        """Small action headers for detecting writes across an external read."""
        return dict(self.conn.execute('SELECT id,status FROM actions WHERE kind=? ORDER BY id DESC', (kind,)))

    @synchronized
    def get_draft(self, signature):
        rows = self._owned_records('WHERE draft_signature IS NOT NULL AND draft_signature=?', (signature,))
        return rows[0] if rows else None

    @synchronized
    def deletion_owner(self, account, remote, status):
        rows = self._owned_records('WHERE status=? AND id=(SELECT MAX(s.action_id) FROM action_targets t '
            'JOIN action_steps s ON s.target_id=t.id JOIN actions a ON a.id=s.action_id '
            "WHERE t.account_id=? AND t.provider_playlist_id_snapshot=? AND s.operation='delete_playlist' AND a.status=?)",
            (status, account, remote, status))
        return rows[0] if rows else None

    @synchronized
    def pending_actions(self, kind):
        return self._owned_records("WHERE kind=? AND status='pending'", (kind,), order='id')

    @synchronized
    def pending_placements(self, membership):
        return self._owned_records("WHERE kind='add_tracks' AND status='pending' AND id IN ("
            "SELECT action_id FROM action_targets WHERE role='placement' AND membership_ref_id=?)", (membership,), order='id')

    @synchronized
    def header(self, identity):
        row = self.conn.execute('SELECT * FROM actions WHERE id=?', (identity,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def prior_intent(self, key):
        row = self.conn.execute('SELECT id,intent_digest FROM actions WHERE idempotency_key=?', (key,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def next_action_id(self):
        return self._next_identity('actions')

    def _next_identity(self, table):
        return self.conn.execute(f'SELECT MAX(COALESCE((SELECT MAX(id) FROM {table}),0),'
            'COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0),'
            "COALESCE((SELECT seq FROM sqlite_sequence WHERE name='collection_file_writes'),0))+1",
            (table,)).fetchone()[0]

    @synchronized
    def reviewed_draft(self, identity, revision):
        row = self.conn.execute('SELECT * FROM actions WHERE id=? AND revision=? AND draft_signature IS NOT NULL',
                                (identity, revision)).fetchone()
        if row is None:
            raise ValueError('The merge draft changed. Reload it before applying the merge.')
        if self.conn.execute('SELECT 1 FROM actions WHERE reviewed_draft_id=? AND reviewed_draft_revision=?', (identity, revision)).fetchone():
            raise ValueError('This draft revision already has an action. Check the Action Log before continuing.')
        return dict(row)

    @synchronized
    def insert_action(self, kind, status, created_at, *, identity, revision, idempotency_key, intent_digest,
                      undo_of, mode, reference, reviewed=None, signature=None):
        self.conn.execute('INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,undo_of_action_id,'
            'execution_mode,created_at,recovery_filename,recovery_digest,recovery_bytes,draft_signature,draft_updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity,kind,status,revision,idempotency_key,intent_digest,undo_of,mode,created_at,
             *reference,signature,created_at if signature is not None else None))
        if reviewed is not None:
            self.conn.execute('UPDATE actions SET reviewed_draft_id=?,reviewed_draft_revision=?,reviewed_draft_signature=? WHERE id=?',
                (reviewed['id'], reviewed['revision'], reviewed['draft_signature'], identity))
        return identity

    @synchronized
    def update_draft(self, identity, revision, now, reference):
        self.conn.execute('UPDATE actions SET revision=?,draft_updated_at=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? WHERE id=?',
            (revision,now,*reference,identity))

    @synchronized
    def update_action(self, identity, status, now, *, revision, reference):
        self.conn.execute('UPDATE actions SET status=?,executed_at=?,revision=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
            'WHERE id=?', (status,now,revision,*reference,identity))

    @synchronized
    def delete_action(self, identity):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM actions WHERE id=?', (identity,))

    @synchronized
    def claim_execution(self, identity, status, now, *, previous_revision, token, revision):
        changed = self.conn.execute('UPDATE actions SET status=?,executed_at=?,execution_token=?,revision=? WHERE id=? AND revision=?',
            (status,now,token,revision,identity,previous_revision)).rowcount
        if changed != 1:
            raise ValueError('Undo was claimed by another request. Reload the Action Log.')

    @synchronized
    def require_execution(self, identity, token):
        if token is None or not self.conn.execute(
                "SELECT 1 FROM actions WHERE id=? AND status='undoing' AND execution_token=?", (identity,token)).fetchone():
            raise ValueError('Undo completion belongs to a stale worker. Reload the Action Log.')

    @synchronized
    def clear_execution(self, identity):
        self.conn.execute('UPDATE actions SET execution_token=NULL WHERE id=?', (identity,))

    @synchronized
    def store_target(self, action, role, position, **values):
        return action_work.target(self.conn, action, role, position, **values)

    @synchronized
    def backups(self, action):
        return action_backup_inventory.records(self.conn, action)

    @synchronized
    def store_backup(self, action, position, reference, retained):
        action_backup_inventory.append(self.conn, action, position, reference, retained)

    @synchronized
    def step_input(self, action, position):
        row = self.conn.execute('SELECT * FROM action_steps WHERE action_id=? AND position=?', (action,position)).fetchone()
        identity = row['id'] if row else self._next_identity('action_steps')
        return identity, dict(row) if row else None

    @synchronized
    def store_step(self, identity, action, target, position, operation, value, phase, outcome, observed, backup, evidence, now):
        created, created_scope = value.get('created_id'), None
        if created is not None:
            owner = self.conn.execute('SELECT account_id FROM action_targets WHERE id=?', (target,)).fetchone()
            if owner and owner[0] is not None:
                created_scope = playlist_scopes.ensure(self.conn, created, owner[0])
        self.conn.execute('INSERT INTO action_steps(id,action_id,target_id,position,operation,phase,outcome,creation_marker,'
            'created_provider_id_snapshot,created_playlist_scope_id,original_survived,observed_status,backup_filename,backup_digest,'
            'backup_bytes,backup_status,updated_at,recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET phase=excluded.phase,outcome=excluded.outcome,backup_filename=excluded.backup_filename,'
            'backup_digest=excluded.backup_digest,backup_bytes=excluded.backup_bytes,backup_status=excluded.backup_status,updated_at=excluded.updated_at,'
            'creation_marker=excluded.creation_marker,created_provider_id_snapshot=excluded.created_provider_id_snapshot,'
            'created_playlist_scope_id=excluded.created_playlist_scope_id,original_survived=excluded.original_survived,observed_status=excluded.observed_status,'
            'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
            (identity,action,target,position,operation,phase,outcome,value.get('marker'),created,created_scope,
             value.get('original_survived'),observed,*backup,now,*evidence))

    @synchronized
    def targets(self, action, role, *, incomplete=False):
        return [dict(row) for row in self.conn.execute('SELECT t.* FROM action_targets t WHERE t.action_id=? AND t.role=? '+
            ("AND EXISTS (SELECT 1 FROM action_steps s WHERE s.target_id=t.id AND s.phase<>'complete') " if incomplete else '')+
            'ORDER BY t.account_id,t.playlist_scope_id,t.id', (action,role))]

    @synchronized
    def target_lock_keys(self, action, role):
        return list(self.conn.execute('SELECT DISTINCT account_id,provider_playlist_id_snapshot FROM action_targets '
            'WHERE action_id=? AND role=? AND account_id IS NOT NULL ORDER BY account_id,provider_playlist_id_snapshot', (action,role)))

    @synchronized
    def uncertain_owner(self, account, remote, finished):
        marks = ','.join('?' for _ in finished)
        row = self.conn.execute('SELECT s.action_id FROM action_targets t JOIN action_steps s ON s.target_id=t.id '
            'JOIN actions a ON a.id=s.action_id WHERE t.account_id=? AND t.provider_playlist_id_snapshot=? '
            f'AND a.status NOT IN ({marks}) '
            "AND (s.outcome='unknown' OR (s.operation='delete_playlist' AND s.phase='pending' "
            "AND s.outcome='not_sent' AND a.idempotency_key='legacy-action:'||a.id)) LIMIT 1", (account,remote,*finished)).fetchone()
        return row[0] if row else None

    @synchronized
    def claim_facts(self, target, finished):
        row = self.conn.execute('SELECT * FROM action_targets WHERE id=?', (target,)).fetchone()
        if row is None:
            return None, None, None
        prior = self.conn.execute('SELECT id,claim_until FROM action_targets WHERE account_id=? AND playlist_scope_id=? '
                                  'AND claim_token IS NOT NULL',
            (row['account_id'],row['playlist_scope_id'])).fetchone()
        uncertain = self.uncertain_owner(row['account_id'],row['provider_playlist_id_snapshot'],finished)
        return dict(row), dict(prior) if prior else None, uncertain

    @synchronized
    def claim_target(self, target, token, until, *, previous=None):
        if previous is not None and previous != target:
            self.conn.execute('UPDATE action_targets SET claim_token=NULL,claim_until=NULL WHERE id=?', (previous,))
        self.conn.execute('UPDATE action_targets SET claim_token=?,claim_until=? WHERE id=?', (token, until, target))

    @synchronized
    def require_claim(self, target, token):
        if not self.conn.execute('SELECT 1 FROM action_targets WHERE id=? AND claim_token=?', (target, token)).fetchone():
            raise ValueError('Action target completion belongs to a stale worker')

    @synchronized
    def deletion_step(self, action, account, remote):
        row = self.conn.execute('SELECT s.* FROM action_steps s JOIN action_targets t ON t.id=s.target_id '
            "WHERE s.action_id=? AND s.operation='delete_playlist' AND t.account_id=? AND t.provider_playlist_id_snapshot=?",
            (action,account,remote)).fetchone()
        return dict(row) if row else None

    @synchronized
    def mark_step(self, identity, phase, outcome):
        self.conn.execute('UPDATE action_steps SET phase=?,outcome=? WHERE id=?', (phase,outcome,identity))

    @synchronized
    def release_deletion(self, claim):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE action_targets SET claim_token=NULL,claim_until=NULL WHERE id=? AND claim_token=?',
                              claim)
