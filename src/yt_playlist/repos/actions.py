"""Action rows, detached recovery references, and conditional SQL claims."""
from contextlib import contextmanager

from yt_playlist.core import action_work, playlist_scopes
from yt_playlist.core.preparation import action_publication, action_ledger
from yt_playlist.repos.base import Repo, synchronized


class ActionRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            yield

    @synchronized
    def has_durable_targets(self):
        return action_work.available(self.conn)

    def _members(self, header, suffix, order):
        return [dict(row) for row in action_publication.members(self.conn, header, suffix, order)]

    def _prepared(self, header):
        return dict(layout='prepared', row=dict(header),
            backups=self._members(header, 'backups', 'position'), steps=self._members(header, 'steps', 'position'))

    def _legacy_action(self, row):
        prepared = None
        owner = action_publication.OWNER
        if self.conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (owner.table('head'),)).fetchone():
            header = self.conn.execute('SELECT g.* FROM '+owner.table('head')+' h JOIN '+owner.table('map')+
                ' m ON m.stage=h.revision AND m.source=? JOIN '+owner.header+
                ' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE h.name=?',
                (str(row['id']), owner.name)).fetchone()
            if header and header['kind'] == row['kind'] and header['created_at'] == row['created_at']:
                prepared = self._prepared(header)
        return dict(layout='legacy', row=dict(row), prepared=prepared)

    def _prepared_records(self, where='', values=(), *, order='action_id DESC', limit=None):
        owner = action_publication.OWNER
        selected = owner.selected(self.conn)
        if selected is None:
            return None
        headers = self.conn.execute('SELECT g.* FROM '+owner.table('map')+' m JOIN '+owner.header+
            ' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=?'+
            (' AND ('+where+')' if where else '')+' ORDER BY '+order+(' LIMIT ?' if limit is not None else ''),
            (selected, *values, *((limit,) if limit is not None else ()))).fetchall()
        return [self._prepared(header) for header in headers]

    def _owned_records(self, where='', values=(), *, order='id DESC'):
        rows = self.conn.execute('SELECT * FROM actions '+where+' ORDER BY '+order, values).fetchall()
        result = []
        for row in rows:
            identity = row['id']
            facts = {name: [dict(item) for item in self.conn.execute(
                'SELECT * FROM action_'+table+' WHERE action_id=?'+order_by, (identity,))]
                for name, table, order_by in (
                    ('targets', 'targets', ' ORDER BY role,position'), ('steps', 'steps', ' ORDER BY position'),
                    ('backups', 'backups', ' ORDER BY position'), ('resolutions', 'plan_resolutions', ' ORDER BY position'),
                    ('items', 'placement_items', ' ORDER BY position'), ('handles', 'placement_handles', ''))}
            draft = self.conn.execute('SELECT * FROM action_drafts WHERE id=?', (identity,)).fetchone()
            placement = self.conn.execute('SELECT * FROM action_placement_intents WHERE action_id=?', (identity,)).fetchone()
            result.append(dict(layout='owned', row=dict(row), **facts,
                draft=dict(draft) if draft else None, placement=dict(placement) if placement else None))
        return result

    @synchronized
    def get_action(self, action_id):
        if self.has_durable_targets():
            rows = self._owned_records('WHERE id=?', (action_id,))
        else:
            rows = self._prepared_records('action_id=?', (action_id,))
            if rows is None:
                row = self.conn.execute('SELECT * FROM actions WHERE id=?', (action_id,)).fetchone()
                return self._legacy_action(row) if row else None
        return rows[0] if rows else None

    @synchronized
    def get_actions(self):
        if self.has_durable_targets():
            return self._owned_records()
        rows = self._prepared_records()
        return rows if rows is not None else [self._legacy_action(row) for row in self.conn.execute('SELECT * FROM actions ORDER BY id DESC')]

    @synchronized
    def get_draft(self, signature):
        if self.has_durable_targets():
            rows = self._owned_records('WHERE id IN (SELECT id FROM action_drafts WHERE signature=?)', (signature,))
        else:
            rows = self._prepared_records("kind='merge_draft' AND draft_signature=?", (signature,), order='action_id', limit=1)
            if rows is None:
                row = self.conn.execute("SELECT * FROM actions WHERE kind='merge_draft' AND plan_json=?", (signature,)).fetchone()
                return self._legacy_action(row) if row else None
        return rows[0] if rows else None

    @synchronized
    def deletion_owner(self, account, remote, status):
        if self.has_durable_targets():
            rows = self._owned_records('WHERE status=? AND id=(SELECT MAX(s.action_id) FROM action_targets t '
                'JOIN action_steps s ON s.target_id=t.id JOIN actions a ON a.id=s.action_id '
                "WHERE t.account_id=? AND t.provider_playlist_id_snapshot=? AND s.operation='delete_playlist' AND a.status=?)",
                (status, account, remote, status))
        else:
            rows = self._prepared_records('status=? AND EXISTS (SELECT 1 FROM '+action_ledger.table('targets')+
                " t WHERE t.source=g.source AND t.preparation_revision=g.preparation_revision AND t.role='deletion' "
                'AND COALESCE(t.account_id,t.unresolved_account_id)=? AND t.provider_playlist_id_snapshot=?)',
                (status, account, remote), limit=1)
            if rows is None:
                row = self.conn.execute('SELECT a.* FROM actions a WHERE a.status=? AND EXISTS ('
                    "SELECT 1 FROM json_each(a.undo_json,'$.deletions') d "
                    "WHERE json_extract(d.value,'$.identity')=? AND json_extract(d.value,'$.ytm')=?) "
                    'ORDER BY a.id DESC LIMIT 1', (status, account, remote)).fetchone()
                return self._legacy_action(row) if row else None
        return rows[0] if rows else None

    @synchronized
    def pending_actions(self, kind):
        if self.has_durable_targets():
            return self._owned_records("WHERE kind=? AND status='pending'", (kind,), order='id')
        rows = self._prepared_records("kind=? AND status='pending'", (kind,), order='action_id')
        return rows if rows is not None else [self._legacy_action(row) for row in self.conn.execute(
            "SELECT * FROM actions WHERE kind=? AND status='pending' ORDER BY id", (kind,))]

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
        return self.conn.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM actions),0),"
            "COALESCE((SELECT seq FROM sqlite_sequence WHERE name='actions'),0))+1").fetchone()[0]

    @synchronized
    def reviewed_draft(self, identity, revision):
        row = self.conn.execute('SELECT * FROM action_drafts WHERE id=? AND revision=?', (identity, revision)).fetchone()
        if row is None:
            raise ValueError('The merge draft changed. Reload it before applying the merge.')
        if self.conn.execute('SELECT 1 FROM actions WHERE reviewed_draft_id=? AND reviewed_draft_revision=?', (identity, revision)).fetchone():
            raise ValueError('This draft revision already has an action. Check the Action Log before continuing.')
        return dict(row)

    @synchronized
    def insert_action(self, kind, params, plan, status, undo, created_at, *, identity=None, revision=None,
                      idempotency_key=None, intent_digest=None, undo_of=None, mode=None, reference=None, reviewed=None):
        if reference is None:
            return self.conn.execute('INSERT INTO actions(kind,params_json,plan_json,undo_json,status,created_at) '
                'VALUES (?,?,?,?,?,?)', (kind, params, plan, undo, status, created_at)).lastrowid
        self.conn.execute('INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,undo_of_action_id,'
            'execution_mode,created_at,recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity,kind,status,revision,idempotency_key,intent_digest,undo_of,mode,created_at,*reference))
        if reviewed is not None:
            self.conn.execute('UPDATE actions SET reviewed_draft_id=?,reviewed_draft_revision=?,reviewed_draft_signature=? WHERE id=?',
                (reviewed['id'], reviewed['revision'], reviewed['signature'], identity))
        return identity

    @synchronized
    def store_draft(self, identity, signature, revision, now, reference):
        self.conn.execute('INSERT INTO action_drafts(id,signature,revision,updated_at,recovery_filename,recovery_digest,recovery_bytes) '
            'VALUES (?,?,?,?,?,?,?)', (identity,signature,revision,now,*reference))

    @synchronized
    def update_draft(self, identity, params, revision, now, reference):
        if reference is None:
            self.conn.execute('UPDATE actions SET params_json=? WHERE id=?', (params, identity))
            return
        self.conn.execute('UPDATE action_drafts SET revision=?,updated_at=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? WHERE id=?',
            (revision,now,*reference,identity))
        self.conn.execute('UPDATE actions SET revision=? WHERE id=?', (revision, identity))

    @synchronized
    def update_action(self, identity, status, now, *, undo=None, params=None, revision=None, reference=None):
        if reference is not None:
            self.conn.execute('UPDATE actions SET status=?,executed_at=?,revision=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                'WHERE id=?', (status,now,revision,*reference,identity))
        else:
            fields, values = ['status=?', 'executed_at=?'], [status, now]
            for field, value in (('undo_json', undo), ('params_json', params)):
                if value is not None:
                    fields.append(field+'=?')
                    values.append(value)
            self.conn.execute('UPDATE actions SET '+','.join(fields)+' WHERE id=?', (*values, identity))

    @synchronized
    def delete_action(self, identity):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM actions WHERE id=?', (identity,))

    @synchronized
    def claim_execution(self, identity, status, now, *, previous_status, previous_revision, token, revision):
        if self.has_durable_targets():
            changed = self.conn.execute('UPDATE actions SET status=?,executed_at=?,execution_token=?,revision=? WHERE id=? AND revision=?',
                (status,now,token,revision,identity,previous_revision)).rowcount
        else:
            changed = self.conn.execute('UPDATE actions SET status=?,executed_at=? WHERE id=? AND status=?',
                (status,now,identity,previous_status)).rowcount
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
        return action_work._target(self.conn, action, role, position, **values)

    @synchronized
    def backups(self, action):
        return [dict(row) for row in self.conn.execute('SELECT * FROM action_backups WHERE action_id=? ORDER BY position', (action,))]

    @synchronized
    def store_backup(self, action, position, reference, retained):
        self.conn.execute('INSERT INTO action_backups(action_id,position,filename,retained_filename,digest,bytes,availability) '
            'VALUES (?,?,?,?,?,?,?)', (action,position,reference[0],retained,*reference[1:]))

    @synchronized
    def step_input(self, action, position):
        row = self.conn.execute('SELECT * FROM action_steps WHERE action_id=? AND position=?', (action,position)).fetchone()
        identity = row['id'] if row else self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM action_steps').fetchone()[0]
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
    def store_placement(self, action, target, videos, handles, after, handles_present):
        if self.conn.execute('SELECT 1 FROM action_placement_intents WHERE action_id=?', (action,)).fetchone():
            return
        self.conn.execute('INSERT INTO action_placement_intents VALUES (?,?,?,?)', (action,target,after,int(handles_present)))
        self.conn.executemany('INSERT INTO action_placement_items VALUES (?,?,?)', [(action,index,video) for index,video in enumerate(videos)])
        self.conn.executemany('INSERT INTO action_placement_handles VALUES (?,?,?)', [(action,video,handle) for video,handle in handles.items()])

    @synchronized
    def store_resolutions(self, action, resolutions):
        if self.conn.execute('SELECT 1 FROM action_plan_resolutions WHERE action_id=?', (action,)).fetchone():
            return
        for list_kind, position, item in resolutions:
            recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (item['identity_key'],)).fetchone()
            self.conn.execute('INSERT INTO action_plan_resolutions VALUES (?,?,?,?,?,?,?,?,?)',
                (action,list_kind,position,recording[0] if recording else None,None if recording else item['identity_key'],
                 item['identity_key'],item['source_video_id'],item['target_video_id'],item['method']))

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
        if self.has_durable_targets():
            row = self.conn.execute('SELECT s.action_id FROM action_targets t JOIN action_steps s ON s.target_id=t.id '
                'JOIN actions a ON a.id=s.action_id WHERE t.account_id=? AND t.provider_playlist_id_snapshot=? '
                f'AND a.status NOT IN ({marks}) '+
                "AND (s.outcome='unknown' OR (s.operation='delete_playlist' AND s.phase='pending' "
                "AND s.outcome='not_sent' AND a.idempotency_key='legacy-action:'||a.id)) LIMIT 1", (account,remote,*finished)).fetchone()
        else:
            row = self.conn.execute("SELECT a.id FROM actions a WHERE a.status='delete_pending' AND EXISTS ("
                "SELECT 1 FROM json_each(a.undo_json,'$.deletions') d WHERE json_extract(d.value,'$.identity')=? "
                "AND json_extract(d.value,'$.ytm')=? AND json_extract(d.value,'$.phase') IN ('pending','uncertain')) LIMIT 1",
                (account,remote)).fetchone()
        return row[0] if row else None

    @synchronized
    def claim_facts(self, target, finished):
        row = self.conn.execute('SELECT * FROM action_targets WHERE id=?', (target,)).fetchone()
        if row is None:
            return None, None, None
        prior = self.conn.execute('SELECT * FROM action_target_claims WHERE account_id=? AND playlist_scope_id=?',
            (row['account_id'],row['playlist_scope_id'])).fetchone()
        uncertain = self.uncertain_owner(row['account_id'],row['provider_playlist_id_snapshot'],finished)
        return dict(row), dict(prior) if prior else None, uncertain

    @synchronized
    def claim_target(self, target, account, scope, token, until):
        self.conn.execute('INSERT INTO action_target_claims VALUES (?,?,?,?,?) ON CONFLICT(account_id,playlist_scope_id) '
            'DO UPDATE SET action_target_id=excluded.action_target_id,claim_token=excluded.claim_token,lease_until=excluded.lease_until',
            (account,scope,target,token,until))
        self.conn.execute('UPDATE action_steps SET claim_token=? WHERE target_id=?', (token,target))

    @synchronized
    def require_claim(self, target, token):
        action_work.require_claim(self.conn,target,token)

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
            self.conn.execute('DELETE FROM action_target_claims WHERE action_target_id=? AND claim_token=?', claim)
