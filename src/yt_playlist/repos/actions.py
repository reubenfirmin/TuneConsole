"""ActionRepo: the undoable action log (merges, moves, dupe-deletes, undos)."""
import hashlib
import json
from pathlib import Path
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Action


class ActionRepo(Repo):
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
        if not is_undoable(action.kind) or action.status not in ("executed", "undo_failed", "undoing"):
            raise ValueError(f"action {action_id} ({action.kind}/{action.status}) is not undoable")
        with self.conn:
            changed = self.conn.execute(
                "UPDATE actions SET status='undoing',executed_at=? WHERE id=? AND status=?",
                (now, action_id, action.status)).rowcount
        if changed != 1:
            raise ValueError("Undo was claimed by another request. Reload the Action Log.")
        return action

    @synchronized
    def save_undo_progress(self, action_id, undo, now, *, failed=False):
        with self.conn:
            changed = self.conn.execute(
                "UPDATE actions SET undo_json=?,status=?,executed_at=? WHERE id=? AND status='undoing'",
                (json.dumps(undo), "undo_failed" if failed else "undoing", now, action_id)).rowcount
        if changed != 1:
            raise ValueError("Undo no longer owns this action. Reload the Action Log.")

    @synchronized
    def finish_undo(self, action_id, undo, now):
        from yt_playlist.util.action_kinds import UNDO
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
    def record_action(self, kind, params_json, plan_json, status, undo_json, created_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO actions(kind,params_json,plan_json,undo_json,status,created_at) "
            "VALUES (?,?,?,?,?,?)", (kind, params_json, plan_json, undo_json, status, created_at))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def get_action(self, action_id) -> Action | None:
        row = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            return None
        return Action(row["id"], row["kind"], row["params_json"], row["plan_json"],
                      row["undo_json"], row["status"], row["created_at"], row["executed_at"])

    @synchronized
    def update_action(self, action_id, status, executed_at, undo_json=None, *, params_json=None) -> None:
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
        row = self.conn.execute("SELECT * FROM actions WHERE kind='merge_draft' AND plan_json=?",
                              (signature,)).fetchone()
        if row is None:
            return None
        return Action(row["id"], row["kind"], row["params_json"], row["plan_json"],
                      row["undo_json"], row["status"], row["created_at"], row["executed_at"])

    @synchronized
    def update_draft(self, action_id, params_json) -> None:
        self.conn.execute("UPDATE actions SET params_json=? WHERE id=?",
                          (params_json, action_id))
        self.conn.commit()

    @synchronized
    def delete_action(self, action_id) -> None:
        self.conn.execute("DELETE FROM actions WHERE id=?", (action_id,))
        self.conn.commit()

    @synchronized
    def get_actions(self) -> list[Action]:
        rows = self.conn.execute("SELECT * FROM actions ORDER BY id DESC").fetchall()
        return [Action(r["id"], r["kind"], r["params_json"], r["plan_json"], r["undo_json"],
                       r["status"], r["created_at"], r["executed_at"]) for r in rows]

    @synchronized
    def get_pending_actions(self, kind) -> list[Action]:
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE kind=? AND status='pending' ORDER BY id", (kind,)).fetchall()
        return [Action(r["id"], r["kind"], r["params_json"], r["plan_json"], r["undo_json"],
                       r["status"], r["created_at"], r["executed_at"]) for r in rows]
