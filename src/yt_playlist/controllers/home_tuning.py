"""Temporary Home controls, including epochs, parameter changes and refresh admission."""
from yt_playlist.rec import mix_tuning


class HomeTuning:
    def __init__(self, settings, model, proposals, ledger, genres, *, adjusting, parameter,
                 set_parameter, epoch, refresh, prepared, picker, expire, reset, fingerprint):
        self.settings, self.model, self.proposals, self.ledger, self.genres = settings, model, proposals, ledger, genres
        self.adjusting, self.parameter, self.set_parameter = adjusting, parameter, set_parameter
        self.epoch, self.refresh, self.prepared, self.picker = epoch, refresh, prepared, picker
        self.expire, self.reset, self.fingerprint = expire, reset, fingerprint

    def genre_request(self, values, now, *, avoid=False):
        if (not isinstance(values, list) or not values or len(values) > 100
                or any(not isinstance(value, str) or not value.strip() or len(value) > 100 for value in values)):
            raise ValueError('Invalid genres')
        if avoid:
            values = {axis[0] for axis in self.genres.feature_axes(values).values()}
        else:
            available = {option['family'] for option in (self.prepared(now) or {}).get('genre_options', [])}
            if not set(values) <= available:
                raise ValueError('Unknown genres')
        with self.adjusting(now):
            self.ledger.refresh_card('cards', max(1, self.parameter('erosion_view_cap')), now)
            self.proposals.put_proposals('home_genre_request', {
                'epoch': self.epoch(), 'avoid_genres' if avoid else 'include_genres': sorted(set(values)),
                **({'held_by_vibe': True} if self.settings.get_setting('home_mix_vibe') else {})}, now)
        self.refresh()

    def breadth(self, bias, now):
        if bias is not None:
            with self.adjusting(now):
                self.set_parameter('breadth_bias', bias)

    def steer(self, axis, target, now):
        if axis and target is not None and axis.split(':', 1)[0] in ('genre', 'era', 'artist'):
            with self.adjusting(now):
                weights = self.model.get_weights(now=now, revert_halflife_d=self.parameter('weight_revert_halflife_d'))
                self.model.set_lean(axis, mix_tuning.lean_for_target(weights.get(axis, 1.), target), now)

    def pin(self, axis, now):
        if axis and axis.split(':', 1)[0] in ('genre', 'era', 'artist'):
            with self.adjusting(now):
                self.model.unhide_facet(axis)
                if self.model.get_lean(axis) == 1. and axis not in self.model.get_leans():
                    self.model.set_lean(axis, 1., now)

    def remove(self, axis, now):
        if axis:
            with self.adjusting(now):
                self.model.hide_facet(axis)
                self.model.clear_lean(axis)

    def active(self):
        request = self.proposals.get_proposals('home_genre_request') or {}
        return tuning_active(self.model.get_leans(), self.model.hidden_facets(),
                             self.settings.get_setting('home_mix_vibe'), self.parameter('breadth_bias'),
                             request, self.epoch())


def tuning_active(leans, hidden, vibe, breadth, request, epoch):
    return bool(leans or hidden or vibe or breadth != 0
                or (request.get('epoch') == epoch and (request.get('include_genres') or request.get('avoid_genres'))))
