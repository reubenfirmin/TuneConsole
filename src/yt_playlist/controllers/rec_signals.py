"""Mood history retention and theme persistence."""
MOOD_EVENT_CAP = 200


class RecommendationSignals:
    def __init__(self, repository):
        self.repository = repository

    def record_mood(self, keys, direction, now, prune_before=None):
        return self.repository.record_mood(keys, direction, now,
                                           cap=MOOD_EVENT_CAP, prune_before=prune_before)

    def recent_mood_events(self, limit=None):
        return self.repository.recent_mood_events(MOOD_EVENT_CAP if limit is None else limit)

    def bump_theme(self, facet, contribution, now):
        return self.repository.bump_theme(facet, contribution, now)

    def discount_theme(self, facet, amount):
        return self.repository.discount_theme(facet, amount)

    def get_theme(self, facet):
        return self.repository.get_theme(facet)

    def theme_rows(self):
        return self.repository.theme_rows()

    def log_graduation(self, axis, source, score, factor, new_weight, now):
        return self.repository.log_graduation(axis, source, score, factor, new_weight, now)

    def recent_graduations(self, limit=50, *, since=None, until=None):
        return self.repository.recent_graduations(limit, since=since, until=until)

    def graduation_audit_rows(self):
        return self.repository.graduation_audit_rows()

    def graduation_log_counts(self):
        return self.repository.graduation_log_counts()

    def get_play_graduated_day(self, axis):
        return self.repository.get_play_graduated_day(axis)

    def set_play_graduated_day(self, axis, day):
        return self.repository.set_play_graduated_day(axis, day)
