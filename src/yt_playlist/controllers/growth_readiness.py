"""Acquire feature-owned readiness facts lazily, then apply detached capacity rules."""
from yt_playlist.rec.readiness_policy import albums_ready, foundation_ready, prepared_taste
from yt_playlist.rec.onboarding_sampling import sample_tracks


class GrowthReadiness:
    def __init__(self, *, modes, contexts, configuration, reserve, album_menu):
        self.modes, self.contexts, self.configuration = modes, contexts, configuration
        self.reserve, self.album_menu = reserve, album_menu

    def foundation(self, capacity):
        modes = self.modes()
        generation, collaborative = (None, None) if modes else self.configuration()
        contexts = []
        if not modes:
            for context in self.contexts():
                contexts.append(context)
                if prepared_taste(context, generation, collaborative):
                    break
        return foundation_ready(modes, contexts, generation, collaborative, self.reserve(), capacity)

    def albums(self, capacity):
        return albums_ready(self.album_menu(), capacity)


class OnboardingSamples:
    def __init__(self, readiness, rng):
        self.readiness, self.rng = readiness, rng

    def sample(self, n=12):
        return sample_tracks(self.readiness.library_sample(n), n, self.rng)
