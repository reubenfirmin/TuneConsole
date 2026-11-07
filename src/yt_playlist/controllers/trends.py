"""Listening-hour interpretation and ordered publication of recap output."""
from yt_playlist.rec.trend_statistics import hour_buckets


class Trends:
    def __init__(self, repository, proposals):
        self.repository, self.proposals = repository, proposals

    def play_hours(self, since, until, exclude_generated=True, *, calendar=False):
        return hour_buckets(self.repository.play_hour_rows(
            since, until, exclude_generated, calendar=calendar), calendar=calendar)

    def publish_rollup(self, rows, encoded_payload, now, imported):
        self.repository.publish_first_play(rows, imported)
        self.proposals.put_encoded('trend_rollups', encoded_payload, now)

    def catalog_artists(self):
        return self.repository.catalog_artists()

    def has_play_ledger(self):
        return self.repository.has_play_ledger()

    def ledger_day_counts(self, exclude_generated=True):
        return self.repository.ledger_day_counts(exclude_generated)

    def ledger_track_plays(self, since, until, exclude_generated=True, *, calendar=False):
        return self.repository.ledger_track_plays(since, until, exclude_generated, calendar=calendar)

    def track_audio(self):
        return self.repository.track_audio()

    def ledger_artist_plays(self, since, until, exclude_generated=True, *, calendar=False):
        return self.repository.ledger_artist_plays(since, until, exclude_generated, calendar=calendar)

    def prepare_first_play(self):
        return self.repository.prepare_first_play()

    def clear_first_play(self):
        return self.repository.clear_first_play()

    def refresh_first_play(self):
        return self.repository.refresh_first_play()

    def first_play_map(self, kind):
        return self.repository.first_play_map(kind)

    def play_day_counts(self):
        return self.repository.play_day_counts()

    def track_meta(self):
        return self.repository.track_meta()

    def artist_labels(self):
        return self.repository.artist_labels()

    def day_artist_plays(self, since, until):
        return self.repository.day_artist_plays(since, until)

    def track_cards(self, keys):
        return self.repository.track_cards(keys)

    def month_track_plays(self, since, until):
        return self.repository.month_track_plays(since, until)
