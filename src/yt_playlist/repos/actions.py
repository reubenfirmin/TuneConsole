"""ActionRepo: the undoable action log (merges, moves, dupe-deletes, undos)."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Action
from yt_playlist.core import action_work


class ActionRepo(Repo):
    @synchronized
    def has_durable_targets(self):
        """Whether action targets retain claims and backups across a restart."""
        return action_work.available(self.conn)

    def _legacy_action(self, row):
        from yt_playlist.core.preparation.action_publication import legacy_undo
        return Action(row['id'], row['kind'], row['params_json'], row['plan_json'], legacy_undo(self.conn, row),
            row['status'], row['created_at'], row['executed_at'])

    def _prepared_records(self, where='', values=(), *, order='action_id DESC', limit=None):
        from yt_playlist.core.preparation.action_publication import records
        rows = records(self.conn, where, values, order=order, limit=limit)
        return None if rows is None else [Action(row['action_id'], row['kind'], value['params'], value['plan'],
            value['undo'], row['status'], row['created_at'], row['executed_at']) for row, value in rows]

    def _owned_records(self,where='',values=(),*,order='id DESC'):
        return [Action(row['id'],row['kind'],value['params'],value['plan'],value['undo'],
                       row['status'],row['created_at'],row['executed_at'],row['revision'],row['execution_token'])
                for row,value in action_work.records(self.conn,where,values,order=order)]

    @synchronized
    def undo_lock_key(self, action_id):
        database = next(r[2] for r in self.conn.execute("PRAGMA database_list") if r[1] == "main")
        database = str(Path(database).resolve()) if database else f"memory-{id(self.conn)}"
        return hashlib.sha256(f"{database}:{action_id}".encode()).hexdigest()

    @synchronized
    def claim_undo(self, action_id, now):
        """Claim under the matching OS lock, including a claim abandoned by a crashed process."""
        action = self.get_action(action_id)
        if action is None:
            raise ValueError(f"no such action {action_id}")
        from yt_playlist.util.action_kinds import is_undoable
        if not is_undoable(action.kind) or action.status not in (
                "executed", "delete_failed", "undo_failed", "undoing"):
            raise ValueError(f"action {action_id} ({action.kind}/{action.status}) is not undoable")
        if action_work.available(self.conn):
            with self.conn:
                changed = self.conn.execute(
                    "UPDATE actions SET status='undoing',executed_at=?,execution_token=?,revision=? "
                    'WHERE id=? AND revision=?',
                    (now,uuid4().hex,uuid4().hex,action_id,action.revision)).rowcount
                if changed!=1:
                    raise ValueError('Undo was claimed by another request. Reload the Action Log.')
            return self.get_action(action_id)
        with self.conn:
            changed = self.conn.execute(
                "UPDATE actions SET status='undoing',executed_at=? WHERE id=? AND status=?",
                (now, action_id, action.status)).rowcount
        if changed != 1:
            raise ValueError("Undo was claimed by another request. Reload the Action Log.")
        return action

    @synchronized
    def save_undo_progress(self, action_id, undo, now, *, failed=False,claim_token=None,target_claims=()):
        if action_work.available(self.conn):
            with self.conn:
                self._require_undo_claim(action_id,claim_token)
                for target,token in target_claims:
                    action_work.require_claim(self.conn,target,token)
                action_work.update(self.conn,action_id,'undo_failed' if failed else 'undoing',now,undo_raw=json.dumps(undo))
            return
        with self.conn:
            changed = self.conn.execute(
                "UPDATE actions SET undo_json=?,status=?,executed_at=? WHERE id=? AND status='undoing'",
                (json.dumps(undo), "undo_failed" if failed else "undoing", now, action_id)).rowcount
        if changed != 1:
            raise ValueError("Undo no longer owns this action. Reload the Action Log.")

    @synchronized
    def finish_undo(self, action_id, undo, now,*,claim_token=None,target_claims=()):
        from yt_playlist.util.action_kinds import UNDO
        if action_work.available(self.conn):
            with self.conn:
                self._require_undo_claim(action_id,claim_token)
                for target,token in target_claims:
                    action_work.require_claim(self.conn,target,token)
                action_work.update(self.conn,action_id,'undone',now,undo_raw=json.dumps(undo))
                self.conn.execute('UPDATE actions SET execution_token=NULL WHERE id=?',(action_id,))
                action_work.insert(self.conn,UNDO,json.dumps({'undid':action_id}),'{}','executed','{}',now,
                                   idempotency_key='undo:'+str(action_id))
            return
        with self.conn:
            changed = self.conn.execute(
                "UPDATE actions SET status='undone',undo_json=?,executed_at=? "
                "WHERE id=? AND status='undoing'", (json.dumps(undo), now, action_id)).rowcount
            if changed != 1:
                raise ValueError("Undo no longer owns this action. Reload the Action Log.")
            self.conn.execute(
                "INSERT INTO actions(kind,params_json,plan_json,undo_json,status,created_at) "
                "VALUES (?,?,?,?,?,?)", (UNDO, json.dumps({"undid": action_id}), "{}", "{}", "executed", now))

    @synchronized
    def record_action(self, kind, params_json, plan_json, status, undo_json, created_at,*,idempotency_key=None,reviewed_draft=None) -> int:
        if action_work.available(self.conn):
            with self.conn:
                return action_work.insert(self.conn,kind,params_json,plan_json,status,undo_json,created_at,
                                          idempotency_key=idempotency_key,reviewed_draft=reviewed_draft)
        cur = self.conn.execute(
            "INSERT INTO actions(kind,params_json,plan_json,undo_json,status,created_at) "
            "VALUES (?,?,?,?,?,?)", (kind, params_json, plan_json, undo_json, status, created_at))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def get_action(self, action_id) -> Action | None:
        if action_work.available(self.conn):
            rows = self._owned_records('WHERE id=?',(action_id,))
            return rows[0] if rows else None
        rows = self._prepared_records('action_id=?', (action_id,))
        if rows is not None:
            return rows[0] if rows else None
        row = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            return None
        return self._legacy_action(row)

    @synchronized
    def update_action(self, action_id, status, executed_at, undo_json=None, *, params_json=None,expected_revision=None,target_claims=()) -> None:
        if action_work.available(self.conn):
            with self.conn:
                self.conn.execute('UPDATE actions SET revision=revision WHERE 0')
                for target,token in target_claims:
                    action_work.require_claim(self.conn,target,token)
                action_work.update(self.conn,action_id,status,executed_at,undo_raw=undo_json,params_raw=params_json,
                                   expected_revision=expected_revision)
            return
        fields, values = ["status=?", "executed_at=?"], [status, executed_at]
        if undo_json is not None:
            fields.append("undo_json=?")
            values.append(undo_json)
        if params_json is not None:
            fields.append("params_json=?")
            values.append(params_json)
        self.conn.execute(f"UPDATE actions SET {', '.join(fields)} WHERE id=?", (*values, action_id))
        self.conn.commit()

    @synchronized
    def get_draft(self, signature: str) -> Action | None:
        if action_work.available(self.conn):
            rows = self._owned_records('WHERE id IN (SELECT id FROM action_drafts WHERE signature=?)',(signature,))
            return rows[0] if rows else None
        rows = self._prepared_records("kind='merge_draft' AND draft_signature=?", (signature,), order='action_id', limit=1)
        if rows is not None:
            return rows[0] if rows else None
        row = self.conn.execute("SELECT * FROM actions WHERE kind='merge_draft' AND plan_json=?",
                              (signature,)).fetchone()
        if row is None:
            return None
        return self._legacy_action(row)

    @synchronized
    def update_draft(self, action_id, params_json) -> None:
        if action_work.available(self.conn):
            with self.conn:
                action_work.update_draft(self.conn,action_id,params_json)
            return
        self.conn.execute("UPDATE actions SET params_json=? WHERE id=?",
                          (params_json, action_id))
        self.conn.commit()

    @synchronized
    def delete_action(self, action_id) -> None:
        self.conn.execute("DELETE FROM actions WHERE id=?", (action_id,))
        self.conn.commit()

    @synchronized
    def get_actions(self) -> list[Action]:
        if action_work.available(self.conn):
            return self._owned_records()
        rows = self._prepared_records()
        if rows is not None:
            return rows
        rows = self.conn.execute("SELECT * FROM actions ORDER BY id DESC").fetchall()
        return [self._legacy_action(row) for row in rows]

    @synchronized
    def get_pending_deletion(self, account_id, provider_playlist_id) -> Action | None:
        """Read only the newest pending owner for this account and remote playlist.

        The partial index excludes completed history before inspecting intent JSON.
        Query afresh for every admission so a collection sweep sees intervening writes.
        """
        return self._get_deletion(account_id, provider_playlist_id, 'delete_pending')

    @synchronized
    def get_failed_deletion(self, account_id, provider_playlist_id) -> Action | None:
        """Find a failed owner for GC recovery without changing manual retry admission."""
        return self._get_deletion(account_id, provider_playlist_id, 'delete_failed')

    def _get_deletion(self, account_id, provider_playlist_id, status):
        if action_work.available(self.conn):
            return self._deletion_owner(account_id, provider_playlist_id, status)
        rows = self._prepared_deletion(account_id, provider_playlist_id, status)
        if rows is not None:
            return rows[0] if rows else None
        row = self.conn.execute(
            "SELECT a.* FROM actions a WHERE a.status=? AND EXISTS ("
            "SELECT 1 FROM json_each(a.undo_json,'$.deletions') d "
            "WHERE json_extract(d.value,'$.identity')=? AND json_extract(d.value,'$.ytm')=?) "
            "ORDER BY a.id DESC LIMIT 1", (status, account_id, provider_playlist_id)).fetchone()
        if row is None:
            return None
        return self._legacy_action(row)

    @synchronized
    def get_pending_actions(self, kind) -> list[Action]:
        if action_work.available(self.conn):
            return self._owned_records("WHERE kind=? AND status='pending'",(kind,),order='id')
        rows = self._prepared_records("kind=? AND status='pending'", (kind,), order='action_id')
        if rows is not None:
            return rows
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE kind=? AND status='pending' ORDER BY id", (kind,)).fetchall()
        return [self._legacy_action(row) for row in rows]

    @synchronized
    def get_pending_placements(self,membership_id):
        if not action_work.available(self.conn):
            return [action for action in self.get_pending_actions('add_tracks')
                    if json.loads(action.plan_json).get('playlist_id')==membership_id]
        return self._owned_records("WHERE kind='add_tracks' AND status='pending' AND id IN ("
            "SELECT action_id FROM action_targets WHERE role='placement' AND membership_ref_id=?)",
            (membership_id,),order='id')

    def _require_undo_claim(self,action_id,token):
        self.conn.execute('UPDATE actions SET revision=revision WHERE 0')
        if token is None or not self.conn.execute(
                "SELECT 1 FROM actions WHERE id=? AND status='undoing' AND execution_token=?",(action_id,token)).fetchone():
            raise ValueError('Undo completion belongs to a stale worker. Reload the Action Log.')

    def _deletion_owner(self,account,remote,status):
        rows = self._owned_records('WHERE status=? AND id=(SELECT MAX(s.action_id) FROM action_targets t '
            'JOIN action_steps s ON s.target_id=t.id JOIN actions a ON a.id=s.action_id '
            "WHERE t.account_id=? AND t.provider_playlist_id_snapshot=? AND s.operation='delete_playlist' AND a.status=?)",
            (status,account,remote,status))
        return rows[0] if rows else None

    def _prepared_deletion(self, account, remote, status):
        from yt_playlist.core.preparation.action_ledger import table
        return self._prepared_records('status=? AND EXISTS (SELECT 1 FROM '+table('targets')+
            " t WHERE t.source=g.source AND t.preparation_revision=g.preparation_revision AND t.role='deletion' "
            'AND COALESCE(t.account_id,t.unresolved_account_id)=? AND t.provider_playlist_id_snapshot=?)',
            (status, account, remote), limit=1)

    @synchronized
    def claim_undo_targets(self,action_id,execution_token,now):
        if not action_work.available(self.conn):
            return []
        with self.conn:
            self._require_undo_claim(action_id,execution_token)
            targets = self.conn.execute('SELECT t.* FROM action_targets t WHERE t.action_id=? '
                "AND t.role='recovery' AND EXISTS (SELECT 1 FROM action_steps s WHERE s.target_id=t.id AND s.phase<>'complete') "
                'ORDER BY t.account_id,t.playlist_scope_id,t.id',(action_id,)).fetchall()
            claims,scopes = [],set()
            for target in targets:
                scope = (target['account_id'],target['playlist_scope_id'])
                if scope in scopes:
                    continue
                scopes.add(scope)
                claims.append((target['id'],action_work.claim_target(self.conn,target['id'],now,reconcile=True)))
            return claims

    @synchronized
    def target_lock_keys(self,action_id,role):
        if not action_work.available(self.conn):
            return []
        return list(self.conn.execute('SELECT DISTINCT account_id,provider_playlist_id_snapshot FROM action_targets '
            'WHERE action_id=? AND role=? AND account_id IS NOT NULL ORDER BY account_id,provider_playlist_id_snapshot',
            (action_id,role)))

    @synchronized
    def require_known_remote_outcome(self, account, remote):
        action_work.require_known_remote_outcome(self.conn, account, remote)

    @synchronized
    def claim_forward_targets(self,action_id,role,now):
        if not action_work.available(self.conn):
            return []
        with self.conn:
            self.conn.execute('UPDATE action_target_claims SET lease_until=lease_until WHERE 0')
            targets = self.conn.execute('SELECT id FROM action_targets WHERE action_id=? AND role=? '
                'ORDER BY account_id,playlist_scope_id,id',(action_id,role)).fetchall()
            return [(target[0],action_work.claim_target(self.conn,target[0],now)) for target in targets]

    @synchronized
    def begin_deletion(self,action_id,account,remote,now,*,reconcile=False):
        if not action_work.available(self.conn):
            return None
        with self.conn:
            self.conn.execute('UPDATE action_steps SET phase=phase WHERE 0')
            step = self.conn.execute('SELECT s.* FROM action_steps s JOIN action_targets t ON t.id=s.target_id '
                "WHERE s.action_id=? AND s.operation='delete_playlist' AND t.account_id=? AND t.provider_playlist_id_snapshot=?",
                (action_id,account,remote)).fetchone()
            if step is None:
                raise ValueError('Deletion has no committed account target and recovery step')
            if not reconcile:
                if step['phase']!='pending' or step['outcome']!='not_sent' or step['backup_status']!='verified':
                    raise ValueError('Deletion requires its verified backup and an unsent intent')
                reference,_ = action_work.backup_reference(step['backup_filename'])
                if reference[:3]!=(step['backup_filename'],step['backup_digest'],step['backup_bytes']):
                    raise ValueError('Deletion backup changed after the intent was committed')
            token = action_work.claim_target(self.conn,step['target_id'],now,reconcile=reconcile)
            if not reconcile:
                self.conn.execute("UPDATE action_steps SET phase='uncertain',outcome='unknown' WHERE id=?",(step['id'],))
            return step['target_id'],token

    @synchronized
    def release_deletion(self,claim):
        if claim is not None:
            with self.conn:
                self.conn.execute('DELETE FROM action_target_claims WHERE action_target_id=? AND claim_token=?',claim)

    @synchronized
    def save_deletion_progress(self,action_id,undo,now,claims):
        from yt_playlist.library.playlist_deletion import deletion_status
        if not action_work.available(self.conn):
            self.update_action(action_id,deletion_status(undo),now,undo_json=json.dumps(undo))
            return
        with self.conn:
            self.conn.execute('UPDATE action_steps SET phase=phase WHERE 0')
            for target,token in claims.values():
                action_work.require_claim(self.conn,target,token)
            action_work.update(self.conn,action_id,deletion_status(undo),now,undo_raw=json.dumps(undo))
