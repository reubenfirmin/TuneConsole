"""Model publication, forecasts, and rollback snapshots.

Selection changes span vectors and settings in one transaction. They live here so
selection/report consumers do not depend on taste feedback or embedding storage.
"""
import json

from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class ModelSelectionRepo(Repo):
    @synchronized
    def latest_daily_selection(self):
        row = self.conn.execute("SELECT id,result_json FROM rec_model_selection_runs "
            "WHERE json_extract(result_json,'$.kind')='daily' ORDER BY id DESC LIMIT 1").fetchone()
        return {"id": row["id"], **json.loads(row["result_json"])} if row else None

    @snapshot_read
    def comparison_activity(self, start, end):
        """One first timestamp per independently played key in a half-open interval."""
        return {row[0]: row[1] for row in self.conn.execute(
            'SELECT identity_key,MIN(at) FROM listening_activity WHERE at>=? AND at<? '
            'GROUP BY identity_key', (start, end))}

    @synchronized
    def maintain_selection(self, now):
        """Bound disposable pair/forecast storage, preserving live and held versions."""
        keep = {str(row[0]) for row in self.conn.execute(
            'SELECT id FROM rec_model_selection_runs ORDER BY id DESC LIMIT 12')}
        for key in ('rec_selection_active_run', 'rec_selection_display_run', 'rec_selection_live_run'):
            value = self._store.settings.get_setting(key)
            if value:
                keep.add(str(value))
        raw = self._store.settings.get_setting('radio_current_session')
        try:
            radio = json.loads(raw) if raw else {}
            if not radio.get('stopped') and (radio.get('comparison') or {}).get('run_id'):
                keep.add(str(radio['comparison']['run_id']))
        except (TypeError, ValueError):
            pass
        for surface in ('home_choice', 'model_album_assignment'):
            held = self._store.rec_proposals.get_proposals(surface) or {}
            assigned = held.get('comparison') or (held.get('choice_context') or {}).get('comparison') or {}
            if assigned.get('run_id'):
                keep.add(str(assigned['run_id']))
        pairs = [row[0] for row in self.conn.execute("SELECT surface FROM rec_proposals WHERE surface LIKE 'model_pair:%'")]
        with self.conn:
            for surface in pairs:
                if surface.split(':')[1] not in keep:
                    self.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
            qs = ','.join('?' for _ in keep) or 'NULL'
            self.conn.execute(f'DELETE FROM rec_model_previous_vectors WHERE run_id NOT IN ({qs})', tuple(keep))
            # Legacy diagnostic forecasts carry full candidate universes. An
            # active restore point protects vectors, not an endless forecast log.
            # Freeze the last published summary before retiring an entire source
            # series; never recompute a misleading partial history from its tail.
            expired = [row[0] for row in self.conn.execute(
                'SELECT DISTINCT run_id FROM rec_model_forecasts WHERE made_at<?', (now - 180 * 86400,))]
            for run_id in expired:
                run = self.selection_run(run_id)
                if run:
                    followup = run.setdefault('followup', {})
                    followup.update(frozen_at=now, reprocessing_available=False,
                                    retention_days=180)
                    self.conn.execute('UPDATE rec_model_selection_runs SET result_json=? WHERE id=?',
                                      (json.dumps(run), run_id))
                self.conn.execute('DELETE FROM rec_model_forecasts WHERE run_id=?', (run_id,))

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
        run = self.selection_run(run_id)
        if run and (run.get('followup') or {}).get('frozen_at') is not None:
            return
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
            self.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
                "('home_prepared','home_tuned','home_choice','album_prepared','mode_bundles','fresh_pool',"
                "'model_live_comparison','model_album_assignment','model_listening_monitor','model_live_report') OR surface LIKE 'model_pair:%'")
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
                if last.get('kind') == 'product' and last.get('status') == 'running':
                    last.update(status='retained', phase='complete',
                                reason='Comparison ended when the previous model was restored.')
                self.conn.execute("UPDATE rec_model_selection_runs SET result_json=? WHERE id=?",
                                  (json.dumps(last), latest["id"]))
        return True

    @synchronized
    def selection_play_keys(self, start, end):
        """Only exact play timestamps can establish outcomes after a saved forecast."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM play_events WHERE played_at>=? AND played_at<?", (start, end))}
