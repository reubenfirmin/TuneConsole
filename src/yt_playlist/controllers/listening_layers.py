"""Capture listening inputs once before calculating the NOW and session layers."""
from yt_playlist.rec import layers


class ListeningLayers:
    def __init__(self, *, parameter, evidence, plays):
        self.parameter, self.evidence, self.plays = parameter, evidence, plays

    def now_mode_mix(self, now, *, evidence=None):
        window = self.parameter('now_window_h')
        captured = evidence if evidence is not None else self.evidence(now, window_h=window)
        return layers.now_mode_mix(captured, now, window_h=window,
                                   min_events=int(self.parameter('now_min_events')))

    def now_mode_posterior(self, now):
        return self.now_mode_mix(now)[0]

    def evidence_coverage(self, now, *, evidence=None):
        window = self.parameter('now_window_h')
        captured = evidence if evidence is not None else self.evidence(now, window_h=max(24, window))
        return layers.evidence_coverage(captured, now, window_h=window)

    def session_mode_mix(self, now, *, evidence=None):
        captured = evidence if evidence is not None else self.evidence(now, window_h=24)
        return layers.session_mode_mix(captured, now,
            min_events=int(self.parameter('now_min_events')),
            halflife_h=self.parameter('session_halflife_h'))

    def session_tilt(self, now, vectors, index):
        latest = {row['identity_key']: row['played_at'] for row in self.plays(now - 24 * 3600)}
        return layers.session_tilt(latest, now, vectors, index,
            min_events=int(self.parameter('now_min_events')),
            halflife_h=self.parameter('session_halflife_h'))
