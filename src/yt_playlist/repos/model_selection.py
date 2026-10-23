"""Model publication, forecasts, and rollback snapshots.

Selection changes span vectors and settings in one transaction. They live here so
selection/report consumers do not depend on taste feedback or embedding storage.
"""
import json

from yt_playlist.repos.base import Repo, synchronized


class ModelSelectionRepo(Repo):
    @synchronized
    def selection_runs(self, limit=6):
        return [{"id": r["id"], **json.loads(r["result_json"])} for r in self.conn.execute(
            "SELECT id, result_json FROM rec_model_selection_runs ORDER BY id DESC LIMIT ?", (limit,))]

    @synchronized
    def selection_run(self, run_id):
        row = self.conn.execute("SELECT id,result_json FROM rec_model_selection_runs WHERE id=?", (run_id,)).fetchone()
        return {"id": row["id"], **json.loads(row["result_json"])} if row else None

    @synchronized
    def start_selection(self, now, result):
        cur = self.conn.execute(
            "INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (?,?,2)",
            (now, json.dumps(result)))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def save_selection(self, run_id, result, *, rows=None):
        """Publish vectors, their configuration, result, and exact rollback snapshot together."""
        with self.conn:
            self.conn.execute('UPDATE rec_model_selection_runs SET result_json=result_json WHERE id=?', (run_id,))
            self.publish_selection(run_id, json.dumps(result), rows, result["selected"])

    @synchronized
    def publish_selection(self, run_id, encoded, rows, selected):
        """Only prepared writes; the caller owns the transaction and stale-input guard."""
        if not self.conn.in_transaction:
            raise RuntimeError('Selection publication requires an outer transaction')
        if rows is not None:
            paused = self.conn.execute("SELECT value FROM settings WHERE key='rec_selection_paused'").fetchone()
            if paused and paused["value"] == "1":
                raise ValueError("Automatic selection was paused before publication")
            self.conn.execute(
                "INSERT INTO rec_model_previous_vectors SELECT ?,identity_key,vec FROM rec_vectors", (run_id,))
            self.conn.execute("DELETE FROM rec_vectors")
            self.conn.executemany("INSERT INTO rec_vectors(identity_key,vec) VALUES (?,?)", rows)
            self._store.settings.publish_values({"rec_embed_method": selected["method"],
                "rec_dim": str(selected["dim"]), "rec_selection_active_run": str(run_id)})
            self.conn.execute("DELETE FROM settings WHERE key='rec_fit_inputs'")
        self.conn.execute("UPDATE rec_model_selection_runs SET result_json=? WHERE id=?", (encoded, run_id))

    @synchronized
    def save_model_forecast(self, run_id, now, forecast):
        self.conn.execute("INSERT OR IGNORE INTO rec_model_forecasts VALUES (?,?,?)",
                          (run_id, now, json.dumps(forecast)))
        self.conn.commit()

    @synchronized
    def model_forecasts(self, run_id):
        return [{"made_at": r["made_at"], **json.loads(r["forecast_json"])} for r in self.conn.execute(
            "SELECT made_at,forecast_json FROM rec_model_forecasts WHERE run_id=? ORDER BY made_at", (run_id,))]

    @synchronized
    def restore_model_selection(self, run_id, now):
        row = self.conn.execute("SELECT result_json,identity_version FROM rec_model_selection_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return False
        if row['identity_version'] != 2:
            raise ValueError('This model predates a catalog identity change. Rebuild a model before restoring it.')
        result = json.loads(row["result_json"])
        if result.get("status") != "applied" or not self.conn.execute(
                "SELECT 1 FROM rec_model_previous_vectors WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            return False
        with self.conn:
            self.conn.execute("DELETE FROM rec_vectors")
            self.conn.execute("INSERT INTO rec_vectors SELECT identity_key,vec FROM rec_model_previous_vectors WHERE run_id=?",
                              (run_id,))
            self.conn.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", [
                ("rec_embed_method", result["previous"]["method"]),
                ("rec_dim", str(result["previous"]["dim"])),
                ("rec_selection_paused", "1"), ("rec_selection_restore", ""), ("rec_selection_active_run", ""),
                ("rec_selection_display_run", str(run_id))])
            result.update(status="restored", restored_at=now, reason="Previous model restored at your request.")
            self.conn.execute("UPDATE rec_model_selection_runs SET result_json=? WHERE id=?",
                              (json.dumps(result), run_id))
            latest = self.conn.execute("SELECT id,result_json FROM rec_model_selection_runs ORDER BY id DESC LIMIT 1").fetchone()
            if latest["id"] != run_id:
                last = json.loads(latest["result_json"])
                last["ended_at"] = now
                self.conn.execute("UPDATE rec_model_selection_runs SET result_json=? WHERE id=?",
                                  (json.dumps(last), latest["id"]))
        return True

    @synchronized
    def selection_play_keys(self, start, end):
        """Only exact play timestamps can establish outcomes after a saved forecast."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM play_events WHERE played_at>=? AND played_at<?", (start, end))}
