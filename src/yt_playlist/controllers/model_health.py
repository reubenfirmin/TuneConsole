"""Prepare and publish diagnostic reports without involving worker scheduling."""
from yt_playlist.core.activity import check_cancellable_work


class ModelHealth:
    def __init__(self, *, snapshot, input_key, calculate, reports, surface):
        self.snapshot, self.input_key, self.calculate = snapshot, input_key, calculate
        self.reports, self.surface = reports, surface

    def prepare(self, now):
        with self.snapshot() as snapshot:
            report = self.calculate(snapshot.store, now, self.input_key(now))
            check_cancellable_work()
            return report if snapshot.publish_ordered(
                lambda: self.reports.put_proposals(self.surface, report, now)) else None
