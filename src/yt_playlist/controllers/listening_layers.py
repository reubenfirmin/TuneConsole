"""Read NOW and TODAY from the same captured listening windows."""
from yt_playlist.rec import layers


class ListeningLayers:
    def __init__(self, *, parameter, evidence, plays):
        self.parameter, self.evidence, self.plays = parameter, evidence, plays

    def now_mode_mix(self, now, *, evidence=None):
        captured = evidence if evidence is not None else self.evidence(now)
        return layers.now_mode_mix(captured, now, window_h=0,
                                   min_events=int(self.parameter('now_min_events')))

    def now_mode_posterior(self, now):
        return self.now_mode_mix(now)[0]

    def evidence_coverage(self, now, *, evidence=None):
        captured = evidence if evidence is not None else self.evidence(now)
        return layers.evidence_coverage(captured, now, window_h=0)

    def session_mode_mix(self, now, *, evidence=None):
        captured = evidence if evidence is not None else self.evidence(now)
        return layers.session_mode_mix(captured, now,
            min_events=int(self.parameter('now_min_events')),
            halflife_h=self.parameter('session_halflife_h'))

    def session_tilt(self, now, vectors, index):
        return self.track_tilts(now, vectors, index)[1]

    def track_tilts(self, now, vectors, index):
        captured = self.plays(now)
        minimum = int(self.parameter('now_min_events'))
        current = (layers.session_tilt(captured.current, now, vectors, index, min_events=minimum,
                                       halflife_h=None, since=float('-inf'))
                   if self.parameter('now_gain') else None)
        today = captured.earlier_today() if current is not None else captured.today
        decayed = self._today_tilt(today, now, vectors, index, minimum) if self.parameter('session_alpha') else None
        return current, decayed

    def _today_tilt(self, latest, now, vectors, index, minimum):
        return layers.session_tilt(latest, now, vectors, index,
            min_events=minimum, halflife_h=self.parameter('session_halflife_h'), since=float('-inf'))
