"""Taste changes and fixed-baseline comparisons over explicit capabilities."""
import json
from datetime import datetime, timezone

from yt_playlist.rec import parameter_specs as rec_params

COMPARISON_SETTING = 'taste_comparison_baseline_v2'


class Taste:
    def __init__(self, settings, model, *, ranked=None, selection=None):
        self.settings, self.model = settings, model
        self.ranked, self.selection = ranked, selection

    def checkpoint(self):
        raw = self.settings.get_setting(rec_params.CHECKPOINT_SETTING)
        saved = json.loads(raw) if raw else None
        if saved:
            saved['saved_at_iso'] = datetime.fromtimestamp(saved['saved_at'], timezone.utc).isoformat()
        return saved

    def save_checkpoint(self, now, *, replace=False):
        self.model.save_taste_settings(rec_params.checkpoint_keys(), now, replace=replace)

    def restore_checkpoint(self):
        self.model.restore_taste_settings(rec_params.checkpoint_keys())

    def set_weight(self, axis, weight, now):
        bounds = dict(lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX) if axis.startswith('genre:') else {}
        self.model.set_session_weight(axis, weight, now, **bounds)

    def set_param(self, name, value, now):
        self.save_checkpoint(now)
        if value not in (None, ''):
            spec = rec_params.PARAMS_BY_NAME[name]
            self.settings.set_setting(rec_params.SETTING_PREFIX + name, str(rec_params._clamp(spec, value)))

    def reset_param(self, name, now):
        self.save_checkpoint(now)
        rec_params.PARAMS_BY_NAME[name]
        self.settings.delete_setting(rec_params.SETTING_PREFIX + name)

    def reset_weights(self, now):
        self.save_checkpoint(now)
        self.model.clear_session_weights()
        self.model.reset_weights()

    def reset_all(self, now):
        self.reset_weights(now)
        for spec in rec_params.PARAMS:
            self.settings.delete_setting(rec_params.SETTING_PREFIX + spec.name)

    def capture_baseline(self, now):
        baseline = compact_baseline(self.ranked(now))
        self.settings.set_setting(COMPARISON_SETTING, json.dumps(baseline))

    def clear_feedback(self):
        self.model.clear_feedback()

    def unban(self, key, now):
        self.model.unban(key, now)

    def reset_album_tuning(self):
        from yt_playlist.core.taste_settings import ALBUM_MENU as SETTING
        self.settings.delete_setting(SETTING)

    def restore_selection(self):
        run = self.selection.active_run_id()
        if not run:
            raise ValueError('There is no model selection to restore.')
        self.selection.request_restore(run)

    def pause_selection(self):
        self.selection.request_pause()

    def resume_selection(self):
        if self.selection.restore_pending():
            raise ValueError('Wait for the previous model to finish restoring.')
        self.selection.request_resume()


def compact_baseline(rows):
    return [{key: row[key] for key in ('key', 'rank', 'score', 'trace', 'lane', 'lane_weight')}
            for row in rows]


def baseline_rows(raw):
    if not raw:
        return None
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return rows if isinstance(rows, list) and all(isinstance(row, dict) and 'key' in row for row in rows) else None


def comparison_rows(baseline, current, weights):
    before = {row['key']: row for row in baseline}
    rows = []
    for captured in current:
        row = dict(captured)
        old = before.get(row['key'])
        row.update(old_rank=old.get('rank') if old else None,
                   old_score=old.get('score') if old else None,
                   old_lane=old.get('lane', '') if old else '',
                   old_lane_weight=old.get('lane_weight') if old else None)
        if row['lane_weight'] is None and row['old_lane']:
            row['lane_weight'] = weights.get(f"lane:{row['old_lane']}", 1.)
        row['status'] = ('entered' if old is None and row['in_cohort'] else
                         'exited' if old and not row['in_cohort'] else 'stayed')
        row['rank_delta'] = row['old_rank'] - row['rank'] if row['old_rank'] and row['rank'] else None
        row['score_delta'] = row['score'] - row['old_score'] if row['old_score'] is not None else None
        row['changes'] = trace_changes(old.get('trace', {}) if old else {}, row['trace'])
        if row['old_lane_weight'] is not None and row['lane_weight'] is not None:
            delta = row['lane_weight'] - row['old_lane_weight']
            if abs(delta) > 1e-9:
                row['changes'].append({'label': 'Recommendation category weight', 'delta': delta,
                    'before': row['old_lane_weight'], 'after': row['lane_weight'], 'multiplier': True})
        row['changes'].sort(key=lambda change: -abs(change['delta']))
        rows.append(row)
    return rows


def trace_changes(old, current):
    fields = (('durable', 'Saved taste match', False),
              ('transient_delta', 'Recent listening adjustment', False),
              ('session_delta', 'Today adjustment', False),
              ('now_delta', 'Current session adjustment', False),
              ('audio_delta', 'Sound adjustment', False),
              ('rank_base', 'Base score after ranking', False),
              ('axis_applied', 'Combined preference multiplier', True))
    changes = []
    for key, label, multiplier in fields:
        if key in old and key in current:
            delta = float(current[key]) - float(old[key])
            if abs(delta) > 1e-9:
                changes.append({'label': label, 'delta': delta, 'before': old[key],
                                'after': current[key], 'multiplier': multiplier})
    old_axes, axes = old.get('axes', {}), current.get('axes', {})
    for axis in sorted(set(old_axes) | set(axes)):
        before, after = float(old_axes.get(axis, 1.)), float(axes.get(axis, 1.))
        if abs(after - before) > 1e-9:
            changes.append({'label': f'{axis.capitalize()} multiplier', 'delta': after - before,
                            'before': before, 'after': after, 'multiplier': True})
    return changes
