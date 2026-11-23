"""Model selection, retention and restoration over explicit persistence owners."""
import json

from yt_playlist.core import recommendation_work
from yt_playlist.core.model_state import RecordingModelUpdate


class ModelSelection:
    def __init__(self, repository, settings, vectors, model, guard):
        self.repository, self.settings = repository, settings
        self.vectors, self.model, self.guard = vectors, model, guard

    def discard_history_models(self):
        return self.repository.delete_history()

    def next_daily_at(self):
        state = self.repository.schedule_state()
        return (state or {}).get('next_at', 0)

    def schedule_daily(self, next_at, now):
        value = {'next_at': next_at}
        recommendation_work.parse('daily_model_schedule', value, now)
        self.repository.publish_schedule(next_at, now)

    def maintain_selection(self, now):
        with self.guard():
            facts = self.repository.retention_facts(12, now - 180 * 86400)
            keep = {str(run) for run in facts['recent']}
            for key in ('rec_selection_active_run', 'rec_selection_display_run'):
                if value := self.settings.get_setting(key):
                    keep.add(str(value))
            frozen = []
            for run in facts['expired']:
                followup = run.setdefault('followup', {})
                followup.update(frozen_at=now, reprocessing_available=False, retention_days=180)
                frozen.append((json.dumps(run), run['id']))
            self.repository.prune(keep, frozen, [run['id'] for run in facts['expired']])

    def save_selection(self, run_id, result, *, rows=None):
        return self.publish_selection(run_id, json.dumps(result), rows, result['selected'])

    def publish_selection(self, run_id, encoded, rows, selected):
        with self.guard(), self.vectors.retained():
            if rows is not None:
                if self.settings.get_setting('rec_selection_paused') == '1':
                    raise ValueError('Automatic selection was paused before publication')
                publication = self.vectors.prepare('rec_vectors', rows, replace=True,
                                                   model_version=f'selection:{run_id}')
                previous = self.vectors.rows('rec_vectors')
                self.repository.save_previous(run_id, previous)
                self.vectors.publish([publication])
                self.model.publish_model_state(recording=RecordingModelUpdate(fit_fingerprint=None),
                    related_settings={'rec_embed_method': selected['method'], 'rec_dim': str(selected['dim']),
                                      'rec_selection_active_run': str(run_id)})
            self.repository.write_result(run_id, encoded)

    def save_model_forecast(self, run_id, now, forecast):
        run = self.selection_run(run_id)
        if run and (run.get('followup') or {}).get('frozen_at') is not None:
            return
        self.repository.insert_forecast(run_id, now, json.dumps(forecast))

    def restore_model_selection(self, run_id, now):
        with self.guard(), self.vectors.retained():
            facts = self.repository.restore_facts(run_id)
            if facts is None:
                return False
            if facts['identity_version'] != 2:
                raise ValueError('This model predates a catalog identity change. Rebuild a model before restoring it.')
            result, rows = facts['result'], facts['vectors']
            applied = result.get('status') == 'applied' or self.settings.get_setting('rec_selection_active_run') == str(run_id)
            if not applied or not rows:
                return False
            publication = self.vectors.prepare('rec_vectors', rows, replace=True,
                                               model_version=f'selection-restore:{run_id}')
            self.vectors.publish([publication])
            self.repository.retire_model_display()
            self.settings.publish_values({'rec_embed_method': result['previous']['method'],
                'rec_dim': str(result['previous']['dim']), 'rec_selection_paused': '1',
                'rec_selection_restore': '', 'rec_selection_active_run': '', 'rec_selection_display_run': str(run_id)})
            result.update(status='restored', restored_at=now, reason='Previous model restored at your request.')
            self.repository.write_result(run_id, json.dumps(result))
            latest = self.selection_runs(limit=1)
            if latest and latest[0]['id'] != run_id:
                last = latest[0]
                last['ended_at'] = now
                self.repository.write_result(last['id'], json.dumps(last))
            return True

    def retire_model_display(self):
        return self.repository.retire_model_display()

    def latest_daily_selection(self):
        return self.repository.latest_daily_selection()

    def selection_runs(self, limit=6):
        return self.repository.selection_runs(limit)

    def selection_run(self, run_id):
        return self.repository.selection_run(run_id)

    def start_selection(self, now, result):
        return self.repository.start_selection(now, result)

    def model_forecasts(self, run_id):
        return self.repository.model_forecasts(run_id)

    def selection_play_keys(self, start, end):
        return self.repository.selection_play_keys(start, end)

    def active_run_id(self):
        return self._run_pointer("rec_selection_active_run")

    def restore_pending(self):
        return bool(self._run_pointer("rec_selection_restore"))

    def _run_pointer(self, key):
        run_id = self.settings.get_setting(key)
        return run_id if run_id and self.selection_run(run_id) else None

    def request_restore(self, run_id):
        """Ask the worker to pause automatic selection and restore `run_id` on its next pass."""
        self.settings.set_setting("rec_selection_paused", "1")
        self.settings.set_setting("rec_selection_restore", run_id)

    def request_pause(self):
        self.settings.set_setting("rec_selection_paused", "1")

    def request_resume(self):
        self.settings.set_setting("rec_selection_paused", "")
