"""Compose eligibility and relationship evidence into training baskets."""
from yt_playlist.rec import evidence_baskets


class RecommendationEvidence:
    def __init__(self, repository, eligibility, genres, *, read_view, read_only):
        self.repository = repository
        self.eligibility = eligibility
        self.genres = genres
        self.read_view = read_view
        self.read_only = read_only

    def _eligible(self):
        return self.repository.eligible_keys(maximum_duration=self.eligibility.maximum_duration(),
            excluded_video_types=evidence_baskets.EXCLUDED_VIDEO_TYPES) - self.eligibility.generated_only_keys()

    def _excluded_playlists(self):
        return self.eligibility.excluded_playlist_ids() | self.eligibility.catchall_playlist_ids()

    def rec_baskets(self, max_album=30, max_session=120):
        if not self.read_only:
            with self.read_view() as reader:
                return reader.rec_evidence.rec_baskets(max_album, max_session)
        good = self._eligible()
        baskets = evidence_baskets.grouped(
            self.repository.playlist_memberships(excluded=self._excluded_playlists()), good)
        baskets += evidence_baskets.grouped(self.repository.album_memberships(), good, cap=max_album)
        baskets += evidence_baskets.grouped(self.repository.artist_memberships(), good, cap=evidence_baskets.ARTIST_CAP)
        baskets += evidence_baskets.grouped(self.repository.session_memberships(), good, cap=max_session)
        facets = self.repository.content_facets()
        axes = self.genres.feature_axes({row['genre'] for row in facets if row['genre']})
        return baskets + evidence_baskets.content(facets, good, axes)

    def rec_baskets_as_of(self, cutoff, max_album=30, max_session=120):
        if not self.read_only:
            with self.read_view() as reader:
                return reader.rec_evidence.rec_baskets_as_of(cutoff, max_album, max_session)
        safe = self.repository.unchanged_playlists(cutoff) - self._excluded_playlists()
        memberships = self.repository.playlist_memberships(included=safe)
        proven = {key for _, key in memberships}
        good = (self.repository.dated_keys(cutoff) | proven) & self._eligible()
        baskets = evidence_baskets.grouped(memberships, good)
        sessions = self.repository.dated_session_memberships(cutoff)
        baskets += evidence_baskets.grouped(sessions, good, cap=max_session)
        coverage = evidence_baskets.historical_coverage(eligible=good, safe_playlists=safe,
            known_playlists=self.repository.known_playlist_count(cutoff),
            session_groups=len({group for group, key in sessions if key in good}))
        return baskets, coverage

    def evaluation_history_bounds(self):
        return self.repository.evaluation_history_bounds()

    def generated_only_keys_between(self, start_ts, end_ts, group='Generated'):
        return self.repository.generated_only_keys_between(start_ts, end_ts, group)
