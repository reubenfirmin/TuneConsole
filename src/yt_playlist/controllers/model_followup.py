"""Capture prospective follow-up intervals and persist detached measurements."""
from yt_playlist.rec import model_forecasts as model_selection


class ModelFollowup:
    def __init__(self, repository, settings, history, evidence, *, forecast):
        self.repository, self.settings, self.history, self.evidence = repository, settings, history, evidence
        self.forecast = forecast

    def tracked(self, latest):
        if latest and latest.get('diagnostic_challenger'):
            return latest
        active = self.settings.get_setting('rec_selection_active_run')
        if active and (run := self.repository.selection_run(active)) is not None:
            return run
        restored = self.settings.get_setting('rec_selection_display_run')
        restored = self.repository.selection_run(restored) if restored else None
        if restored and (not latest or latest['ran_at'] <= restored.get('restored_at', 0)):
            return restored
        return latest

    def update(self, now, *, now_fn=None):
        runs = self.repository.selection_runs(limit=1)
        run = self.tracked(runs[0] if runs else None)
        if (not run or run['status'] not in ('applied', 'retained', 'restored')
                or run.get('kind') == 'daily'
                or (run.get('followup') or {}).get('frozen_at') is not None):
            return
        forecasts = self.repository.model_forecasts(run['id'])
        end = min(now, run.get('restored_at', now), run.get('ended_at', now))
        measurement = model_selection.Followup()
        for i, start, stop in model_selection.followup_intervals(forecasts, end):
            plays = self.repository.selection_play_keys(start, stop)
            prior = self.history.history_keys_before(start) | self.repository.selection_play_keys(0, start)
            generated = self.evidence.generated_only_keys_between(start, stop)
            measurement.observe(forecasts[i], stop, plays, prior, generated)
        run['followup'] = measurement.summary(len(forecasts), now)
        self.repository.save_selection(run['id'], run)
        if (run['status'] != 'restored' and not run.get('ended_at')
                and self.settings.get_setting('rec_selection_paused') != '1'
                and (not forecasts or now - forecasts[-1]['made_at'] >= model_selection.DAY)):
            forecast = self.forecast(run['previous'], run.get('diagnostic_challenger', run['selected']), now)
            if forecast:
                # The forecast's observation clock is after its fit, never the
                # captured calculation start.
                self.repository.save_model_forecast(run['id'], now_fn() if now_fn else now, forecast)
                run['followup']['forecasts'] = len(forecasts) + 1
                self.repository.save_selection(run['id'], run)
