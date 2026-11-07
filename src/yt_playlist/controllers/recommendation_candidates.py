"""Candidate policy and eligibility composed above bounded SQL ranking."""
from yt_playlist.rec.candidate_policy import (revisit_criteria, comfort_recency_seconds,
    COMFORT_ORDER, ROTATION_ORDER, DEEP_CUT_ORDER, PLAYLIST_ORDER)


class RecommendationCandidates:
    def __init__(self, repository, eligibility):
        self.repository, self.eligibility = repository, eligibility

    def revisit_candidates(self, now, *, keys=None, limit=90):
        return self.repository.revisit_candidates(
            revisit_criteria(now), maximum_duration=self.eligibility.maximum_duration(),
            keys=keys, limit=limit)

    def comfort_candidates(self, now, min_plays=4, recency_full_days=30, limit=50):
        return self.repository.comfort_candidates(
            now, min_plays=min_plays, full_seconds=comfort_recency_seconds(recency_full_days),
            maximum_duration=self.eligibility.maximum_duration(), order=COMFORT_ORDER, limit=limit)

    def more_like_rotation(self, seed_limit=40, limit=40):
        return self.repository.more_like_rotation(
            seed_limit=seed_limit, limit=limit, maximum_plays=1, order=ROTATION_ORDER,
            maximum_duration=self.eligibility.maximum_duration(),
            excluded_playlists=self.eligibility.excluded_playlist_ids())

    def deep_cuts(self, limit=40):
        return self.repository.deep_cuts(limit=limit, minimum_artist_plays=0, order=DEEP_CUT_ORDER,
                                        maximum_duration=self.eligibility.maximum_duration())

    def complete_playlist(self, playlist_id, limit=20):
        return self.repository.complete_playlist(
            playlist_id, limit=limit, artist_weight=2, order=PLAYLIST_ORDER,
            maximum_duration=self.eligibility.maximum_duration())
