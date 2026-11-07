"""SQL records for model comparisons, forecast series and restore points."""
import json

from yt_playlist.core import recommendation_work
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read


class ModelSelectionRepo(Repo):
    @synchronized
    def delete_history(self):
        with self.conn.atomic():
            if self.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_heads'").fetchone():
                self.conn.execute('DELETE FROM model_selection_heads')
            for table in ('rec_model_previous_vectors', 'rec_model_forecasts', 'rec_model_selection_runs'):
                self.conn.execute(f'DELETE FROM {table}')

    @snapshot_read
    def retained_pair_artifacts(self):
        protected = ({row[0] for row in self.conn.execute('SELECT artifact FROM model_pair_contexts')}
                     if self._store._model_projections else set())
        return protected, [row[0] for row in self.conn.execute(
            "SELECT surface FROM rec_proposals WHERE surface LIKE 'model_pair:%'")]

    @snapshot_read
    def comparison_constraints(self):
        from yt_playlist.core import taste_axes
        values = [tuple(row) for row in self.conn.execute(
            "SELECT key,value FROM settings WHERE key LIKE 'rec_param:%' "
            "OR key IN ('album_feedback','rec_cooc_weighting') ORDER BY key")]
        standing = [tuple(row) for row in self.conn.execute(
            taste_axes.projection(self.conn, 'rec_lean', 'axis,value') + ' ORDER BY axis')]
        return values, standing

    @synchronized
    def retire_model_display(self):
        from yt_playlist.core.recommendation_invalidation import clear_model_display
        with self.conn.atomic():
            self.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('home_choice','mode_bundles','fresh_pool')")
            clear_model_display(self.conn)

    @lookup_read
    def schedule_state(self):
        available = recommendation_work.available(self.conn)
        return available, recommendation_work.read(self.conn, 'daily_model_schedule') if available else None

    @synchronized
    def publish_schedule(self, next_at, now):
        if not recommendation_work.available(self.conn):
            return False
        with self.conn.atomic():
            recommendation_work.write(self.conn, 'daily_model_schedule', {'next_at': next_at}, now)
        return True

    @lookup_read
    def latest_daily_selection(self):
        row = self.conn.execute("SELECT id,result_json FROM rec_model_selection_runs "
            "WHERE json_extract(result_json,'$.kind')='daily' ORDER BY id DESC LIMIT 1").fetchone()
        return {'id': row['id'], **json.loads(row['result_json'])} if row else None

    @snapshot_read
    def comparison_activity(self, start, end):
        return {row[0]: row[1] for row in self.conn.execute(
            'SELECT identity_key,MIN(at) FROM listening_activity WHERE at>=? AND at<? '
            'GROUP BY identity_key', (start, end))}

    @snapshot_read
    def retention_facts(self, limit, cutoff):
        from yt_playlist.core import recommendation_setting_outputs
        expired = [row[0] for row in self.conn.execute(
            'SELECT DISTINCT run_id FROM rec_model_forecasts WHERE made_at<?', (cutoff,))]
        return {'recent': [row[0] for row in self.conn.execute(
                    'SELECT id FROM rec_model_selection_runs ORDER BY id DESC LIMIT ?', (limit,))],
                'radio_seeds': [row[0] for row in self.conn.execute(
                    'SELECT run_ref FROM radio_seed_output WHERE run_ref IS NOT NULL')]
                    if recommendation_setting_outputs.available(self.conn) else [],
                'pairs': [row[0] for row in self.conn.execute(
                    "SELECT surface FROM rec_proposals WHERE surface LIKE 'model_pair:%'")],
                'expired': [run for run_id in expired if (run := self.selection_run(run_id)) is not None],
                'expired_ids': expired}

    @synchronized
    def prune(self, keep, surfaces, frozen, expired):
        with self.conn.atomic():
            if self._store._model_projections:
                from yt_playlist.core import model_projections
                model_projections.clear(self.conn, keep)
            self.conn.executemany('DELETE FROM rec_proposals WHERE surface=?', [(surface,) for surface in surfaces])
            qs = ','.join('?' for _ in keep) or 'NULL'
            self.conn.execute(f'DELETE FROM rec_model_previous_vectors WHERE run_id NOT IN ({qs})', tuple(keep))
            self.conn.executemany('UPDATE rec_model_selection_runs SET result_json=? WHERE id=?', frozen)
            self.conn.executemany('DELETE FROM rec_model_forecasts WHERE run_id=?', [(run,) for run in expired])

    @lookup_read
    def selection_runs(self, limit=6):
        return [{'id': row['id'], **json.loads(row['result_json'])} for row in self.conn.execute(
            'SELECT id,result_json FROM rec_model_selection_runs ORDER BY id DESC LIMIT ?', (limit,))]

    @lookup_read
    def selection_run(self, run_id):
        row = self.conn.execute('SELECT id,result_json FROM rec_model_selection_runs WHERE id=?', (run_id,)).fetchone()
        return {'id': row['id'], **json.loads(row['result_json'])} if row else None

    @synchronized
    def start_selection(self, now, result):
        with self.conn.atomic():
            cur = self.conn.execute(
                'INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (?,?,2)',
                (now, json.dumps(result)))
        return cur.lastrowid

    @synchronized
    def save_previous(self, run_id, rows):
        with self.conn.atomic():
            if self.conn.execute('SELECT 1 FROM rec_model_previous_vectors WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
                return
            self.conn.executemany('INSERT INTO rec_model_previous_vectors VALUES (?,?,?)',
                                  [(run_id, key, vector) for key, vector in rows])

    @synchronized
    def write_result(self, run_id, encoded):
        self.conn.execute('UPDATE rec_model_selection_runs SET result_json=? WHERE id=?', (encoded, run_id))
        self.conn.commit()

    @synchronized
    def insert_forecast(self, run_id, now, encoded):
        self.conn.execute('INSERT OR IGNORE INTO rec_model_forecasts VALUES (?,?,?)', (run_id, now, encoded))
        self.conn.commit()

    @lookup_read
    def model_forecasts(self, run_id):
        return [{'made_at': row['made_at'], **json.loads(row['forecast_json'])} for row in self.conn.execute(
            'SELECT made_at,forecast_json FROM rec_model_forecasts WHERE run_id=? ORDER BY made_at', (run_id,))]

    @snapshot_read
    def restore_facts(self, run_id):
        row = self.conn.execute('SELECT result_json,identity_version FROM rec_model_selection_runs WHERE id=?',
                                (run_id,)).fetchone()
        return {'identity_version': row['identity_version'], 'result': json.loads(row['result_json']),
                'vectors': [tuple(row) for row in self.conn.execute(
                    'SELECT identity_key,vec FROM rec_model_previous_vectors WHERE run_id=?', (run_id,))]} if row else None

    @synchronized
    def clear_restored_outputs(self):
        from yt_playlist.core.recommendation_invalidation import clear_model_display
        with self.conn.atomic():
            self.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
                "('home_choice','mode_bundles','fresh_pool','model_live_comparison','model_album_assignment',"
                "'model_listening_monitor','model_live_report') OR surface LIKE 'model_pair:%'")
            if self._store._model_comparison_state:
                from yt_playlist.core import model_comparison_state
                model_comparison_state.clear(self.conn)
            if self._store._model_projections:
                from yt_playlist.core import model_projections
                model_projections.clear(self.conn)
            clear_model_display(self.conn)
            if self._store._recommendation_reports:
                from yt_playlist.core import recommendation_reports
                recommendation_reports.clear(self.conn, ('model_live_report',))

    @lookup_read
    def selection_play_keys(self, start, end):
        return {row['identity_key'] for row in self.conn.execute(
            'SELECT DISTINCT identity_key FROM play_event_subjects WHERE played_at>=? AND played_at<?', (start, end))}
