"""Recommendation admission rules composed from catalog and feedback facts."""
from yt_playlist.core.recommendation_settings import MAX_TRACK_MINUTES


class RecommendationEligibility:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings

    def maximum_duration(self):
        return 60 * MAX_TRACK_MINUTES.parse(
            self.settings.get_setting('rec_param:' + MAX_TRACK_MINUTES.name))

    def track_length_clause(self, col='duration_s'):
        return f'({col} IS NULL OR {col} <= {self.maximum_duration()})'

    def suppressed_candidate_keys(self, keys, now):
        return self.repository.suppressed_candidate_keys(
            keys, now, surface='for_you', scope='', track_kinds=('dismiss', 'mute', 'not_now'),
            global_kind='dislike', artist_kind='mute')

    def catchall_playlist_ids(self, size_floor=120):
        return self.repository.catchall_playlist_ids(size_floor)

    def offered_tracks(self, videos):
        return self.repository.offered_tracks(videos)

    def offer_metadata(self, videos):
        return self.repository.offer_metadata(videos)

    def excluded_playlist_ids(self, group='Generated'):
        return self.repository.excluded_playlist_ids(group)

    def generated_track_keys(self, group='Generated'):
        return self.repository.generated_track_keys(group)

    def generated_only_keys(self, group='Generated'):
        return self.repository.generated_only_keys(group)
