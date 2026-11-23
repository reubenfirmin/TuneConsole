"""SQL records for model comparisons, forecast series and restore points."""
import json

from yt_playlist.core import model_restore_artifact, recommendation_work
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read


class ModelSelectionRepo(Repo):
    @synchronized
    def delete_history(self):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM rec_model_selection_runs')

    @synchronized
    def retire_model_display(self):
        from yt_playlist.core.recommendation_invalidation import clear_model_display
        with self.conn.atomic():
            clear_model_display(self.conn)

    @lookup_read
    def schedule_state(self):
        with setting_reads(self._store, ('daily_model_schedule',)):
            return recommendation_work.read(self.conn, 'daily_model_schedule')

    @synchronized
    def publish_schedule(self, next_at, now):
        with self.conn.atomic():
            recommendation_work.write(self.conn, 'daily_model_schedule', {'next_at': next_at}, now)

    @lookup_read
    def latest_daily_selection(self):
        row = self.conn.execute("SELECT id,result_json FROM rec_model_selection_runs "
            "WHERE json_extract(result_json,'$.kind')='daily' ORDER BY id DESC LIMIT 1").fetchone()
        return {'id': row['id'], **json.loads(row['result_json'])} if row else None

    @snapshot_read
    def retention_facts(self, limit, cutoff):
        expired = [row[0] for row in self.conn.execute(
            "SELECT id FROM rec_model_selection_runs WHERE json_extract(forecast_series,'$[0][0]')<?", (cutoff,))]
        return {'recent': [row[0] for row in self.conn.execute(
                    'SELECT id FROM rec_model_selection_runs ORDER BY id DESC LIMIT ?', (limit,))],
                'expired': [run for run_id in expired if (run := self.selection_run(run_id)) is not None]}

    @synchronized
    def prune(self, keep, frozen, expired):
        with self.conn.atomic():
            qs = ','.join('?' for _ in keep) or 'NULL'
            self.conn.execute(f'UPDATE rec_model_selection_runs SET restore_artifact=NULL '
                              f'WHERE restore_artifact IS NOT NULL AND id NOT IN ({qs})', tuple(keep))
            self.conn.executemany('UPDATE rec_model_selection_runs SET result_json=? WHERE id=?', frozen)
            self.conn.executemany("UPDATE rec_model_selection_runs SET forecast_series='[]' WHERE id=?",
                                  [(run,) for run in expired])

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
        if result.get('kind') == 'product':
            raise ValueError('Live recommendation trials are retired')
        with self.conn.atomic():
            cur = self.conn.execute(
                'INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (?,?,2)',
                (now, json.dumps(result)))
        return cur.lastrowid

    @synchronized
    def save_previous(self, run_id, rows):
        with self.conn.atomic():
            run = self.conn.execute('SELECT restore_artifact IS NOT NULL FROM rec_model_selection_runs WHERE id=?',
                                    (run_id,)).fetchone()
            if run and run[0]:
                return
            payload = model_restore_artifact.pack(sorted(rows))
            if payload is None:
                return
            if run is None:
                raise ValueError('Model restore artifact has no selection run')
            self.conn.execute('UPDATE rec_model_selection_runs SET restore_artifact=? WHERE id=?', (payload, run_id))

    @synchronized
    def write_result(self, run_id, encoded):
        self.conn.execute('UPDATE rec_model_selection_runs SET result_json=? WHERE id=?', (encoded, run_id))
        self.conn.commit()

    @synchronized
    def insert_forecast(self, run_id, now, encoded):
        with self.conn.atomic():
            row = self.conn.execute('SELECT forecast_series FROM rec_model_selection_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('Model forecast has no selection run')
            series = json.loads(row[0])
            if any(at == now for at, _ in series):
                return
            series.append([now, encoded])
            series.sort(key=lambda entry: entry[0])
            self.conn.execute('UPDATE rec_model_selection_runs SET forecast_series=? WHERE id=?', (json.dumps(series), run_id))

    @lookup_read
    def model_forecasts(self, run_id):
        row = self.conn.execute('SELECT forecast_series FROM rec_model_selection_runs WHERE id=?', (run_id,)).fetchone()
        return [{'made_at': at, **json.loads(value)} for at, value in json.loads(row[0])] if row else []

    @snapshot_read
    def restore_facts(self, run_id):
        row = self.conn.execute('SELECT result_json,identity_version,restore_artifact FROM rec_model_selection_runs '
                                'WHERE id=?', (run_id,)).fetchone()
        return {'identity_version': row['identity_version'], 'result': json.loads(row['result_json']),
                'vectors': model_restore_artifact.unpack(row['restore_artifact'])} if row else None

    @lookup_read
    def selection_play_keys(self, start, end):
        return {row['identity_key'] for row in self.conn.execute(
            'SELECT DISTINCT identity_key FROM play_event_subjects WHERE played_at>=? AND played_at<?', (start, end))}
