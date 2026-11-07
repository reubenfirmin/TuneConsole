"""Resolve onboarding evidence criteria before reading catalog facts."""
from yt_playlist.rec import readiness_criteria as criteria


class Readiness:
    def __init__(self, repository, eligibility):
        self.repository, self.eligibility = repository, eligibility

    def familiar_albums(self):
        return self.repository.familiar_albums(
            minimum_songs=criteria.FAMILIAR_SONGS, minimum_plays=criteria.FAMILIAR_PLAYS)

    def library_sample(self, per_genre):
        return self.repository.library_sample(per_genre, maximum_duration=self.eligibility.maximum_duration())

    def playable_owned(self, *, exclude_generated=False):
        return self.repository.playable_owned(
            exclude_generated=exclude_generated, maximum_duration=self.eligibility.maximum_duration())

    def has_vibe_evidence(self, prefix):
        return self.repository.has_vibe_evidence(
            prefix, sources=criteria.MEASURED_VIBE_SOURCES + criteria.PROXY_VIBE_SOURCES,
            minimum_support=criteria.VIBE_MINIMUM_SUPPORT)

    def vibe_evidence(self, prefix):
        return self.repository.vibe_evidence(
            prefix, measured_sources=criteria.MEASURED_VIBE_SOURCES,
            proxy_sources=criteria.PROXY_VIBE_SOURCES, minimum_support=criteria.VIBE_MINIMUM_SUPPORT)

    def journey_history(self):
        return self.repository.journey_history(provider='lastfm', completed_statuses=criteria.COMPLETED_LASTFM_STATUSES)

    def arrival(self):
        return self.repository.arrival()

    def live_starter_played(self, videos):
        return self.repository.live_starter_played(videos)

    def coverage(self):
        return self.repository.coverage()
