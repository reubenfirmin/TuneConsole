"""Apply graduation decisions through narrow model and signal capabilities."""
import math

from yt_playlist.rec import graduation_rules as graduation, parameter_specs as rec_params


class Graduation:
    def __init__(self, signals, model, *, parameter):
        self.signals, self.model, self.parameter = signals, model, parameter

    def _graduate(self, axis, score, now, source_label):
        threshold = self.parameter('theme_threshold')
        factor = graduation.graduation_factor(score, threshold,
            up_factor=self.parameter('graduate_up'), down_factor=self.parameter('graduate_down'))
        if factor is None:
            return None
        weight = self.model.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
            now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
        self.signals.discount_theme(axis, math.copysign(threshold, score))
        self.signals.log_graduation(axis, source_label, score, factor, weight, now)
        return weight

    def facet(self, axis, signed, now, *, source=1., source_label='event'):
        score = self.signals.bump_theme(axis, signed * source, now)
        self._graduate(axis, score, now, source_label)

    def play_exposure(self, leans, now):
        today = graduation.utc_day(now)
        for axis, lean in leans.items():
            if self.signals.get_play_graduated_day(axis) == today:
                continue
            self.signals.set_play_graduated_day(axis, today)
            if not lean:
                continue
            score = self.signals.bump_theme(axis, abs(lean) * self.parameter('source_w_play'), now)
            self._graduate(axis, score, now, 'play')

    def slider_exposure(self, rows, now):
        today = graduation.utc_day(now)
        threshold = self.parameter('theme_threshold')
        strength = self.parameter('source_w_slider')
        for row in rows:
            axis, value = row['axis'], row['value']
            if row['last_graduated_day'] == today:
                continue
            self.model.set_lean_graduated_day(axis, today)
            magnitude = abs(value - 1.)
            if not magnitude:
                continue
            score = self.signals.bump_theme(axis, math.copysign(magnitude * strength, value - 1.), now)
            factor = graduation.graduation_factor(score, threshold,
                up_factor=self.parameter('graduate_up'), down_factor=self.parameter('graduate_down'))
            if factor is None:
                continue
            halflife = self.parameter('weight_revert_halflife_d')
            old = self.model.get_weights(now=now, revert_halflife_d=halflife).get(axis, 1.)
            new = self.model.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                now=now, revert_halflife_d=halflife)
            self.model.set_lean(axis, graduation.migrated_lean(value, old, new), now)
            self.signals.discount_theme(axis, math.copysign(threshold, score))
            self.signals.log_graduation(axis, 'slider', score, factor, new, now)
