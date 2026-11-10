"""Model selection, retention and restoration over explicit persistence owners."""
import json

from yt_playlist.core import recommendation_work


class ModelSelection:
    def __init__(self, repository, settings, proposals, radio, vectors, guard):
        self.repository, self.settings, self.proposals = repository, settings, proposals
        self.radio, self.vectors, self.guard = radio, vectors, guard

    def discard_history_models(self):
        return self.repository.delete_history()

    def retire_product_trial(self, now, reason):
        with self.guard():
            current = self.proposals.get_proposals('model_live_comparison') or {}
            if current.get('phase') not in ('preparing', 'observing'):
                return
            run = self.selection_run(current['run_id'])
            if run:
                run.update(status='retained', phase='complete', reason=reason, ended_at=now)
                self.repository.write_result(run['id'], json.dumps(run))
            current.update(phase='retained', reason=reason, ended_at=now)
            self.proposals.put_proposals('model_live_comparison', current, now)
            self.settings.publish_values({'rec_selection_live_run': ''})
            monitor = self.proposals.get_proposals('model_listening_monitor')
            if monitor:
                self.proposals.put_proposals('model_listening_monitor', {**monitor, 'complete': True}, now)

    def next_daily_at(self):
        available, state = self.repository.schedule_state()
        if not available:
            state = self.proposals.get_proposals('daily_model_schedule')
        return (state or {}).get('next_at', 0)

    def schedule_daily(self, next_at, now):
        value = {'next_at': next_at}
        recommendation_work.parse('daily_model_schedule', value, now)
        if not self.repository.publish_schedule(next_at, now):
            self.proposals.put_proposals('daily_model_schedule', value, now)

    def maintain_selection(self, now):
        with self.guard():
            facts = self.repository.retention_facts(12, now - 180 * 86400)
            keep = {str(run) for run in (*facts['recent'], *facts['radio_seeds'])}
            for key in ('rec_selection_active_run', 'rec_selection_display_run', 'rec_selection_live_run'):
                if value := self.settings.get_setting(key):
                    keep.add(str(value))
            try:
                radio = self.radio.load()[0] or {}
                if not radio.get('stopped') and (radio.get('comparison') or {}).get('run_id'):
                    keep.add(str(radio['comparison']['run_id']))
            except (TypeError, ValueError):
                pass
            for surface in ('home_choice', 'model_album_assignment'):
                held = self.proposals.get_proposals(surface) or {}
                assigned = held.get('comparison') or (held.get('choice_context') or {}).get('comparison') or {}
                if assigned.get('run_id'):
                    keep.add(str(assigned['run_id']))
            frozen = []
            for run in facts['expired']:
                followup = run.setdefault('followup', {})
                followup.update(frozen_at=now, reprocessing_available=False, retention_days=180)
                frozen.append((json.dumps(run), run['id']))
            self.repository.prune(keep, [surface for surface in facts['pairs'] if surface.split(':')[1] not in keep],
                                  frozen, facts['expired_ids'])

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
                self.settings.publish_values({'rec_embed_method': selected['method'], 'rec_dim': str(selected['dim']),
                                               'rec_selection_active_run': str(run_id), 'rec_fit_inputs': None})
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
            self.repository.clear_restored_outputs()
            self.settings.publish_values({'rec_embed_method': result['previous']['method'],
                'rec_dim': str(result['previous']['dim']), 'rec_selection_paused': '1',
                'rec_selection_restore': '', 'rec_selection_active_run': '', 'rec_selection_display_run': str(run_id)})
            result.update(status='restored', restored_at=now, reason='Previous model restored at your request.')
            self.repository.write_result(run_id, json.dumps(result))
            latest = self.selection_runs(limit=1)
            if latest and latest[0]['id'] != run_id:
                last = latest[0]
                last['ended_at'] = now
                if last.get('kind') == 'product' and last.get('status') == 'running':
                    last.update(status='retained', phase='complete',
                                reason='Comparison ended when the previous model was restored.')
                self.repository.write_result(last['id'], json.dumps(last))
            return True

    def retained_pair_artifacts(self):
        return self.repository.retained_pair_artifacts()

    def comparison_constraints(self):
        return self.repository.comparison_constraints()

    def retire_model_display(self):
        return self.repository.retire_model_display()

    def latest_daily_selection(self):
        return self.repository.latest_daily_selection()

    def comparison_activity(self, start, end):
        return self.repository.comparison_activity(start, end)

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
