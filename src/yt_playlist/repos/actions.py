"""ActionRepo — the undoable action log (merges, moves, dupe-deletes, undos)."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Action


class ActionRepo(Repo):
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
    def update_action(self, action_id, status, executed_at, undo_json=None) -> None:
        if undo_json is None:
            self.conn.execute("UPDATE actions SET status=?, executed_at=? WHERE id=?",
                              (status, executed_at, action_id))
        else:
            self.conn.execute("UPDATE actions SET status=?, executed_at=?, undo_json=? WHERE id=?",
                              (status, executed_at, undo_json, action_id))
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
