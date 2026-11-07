"""Capture observations and record receipts after their projection succeeds."""
from contextlib import contextmanager


class PlaybackFeedback:
    def __init__(self, repository, queue, store):
        self.repository, self.queue, self.store = repository, queue, store

    def capture(self, kind, identity, body, now):
        return self.queue.capture(kind, identity, body, now)

    def pending(self):
        return self.queue.pending()

    def queued_rating(self, identity, video):
        return self.queue.rating(identity, video)

    def next_observation(self):
        return self.queue.next()

    def acknowledge_observation(self, capture):
        self.queue.acknowledge(capture)

    @contextmanager
    def replaying(self):
        admitted = not self.store.activity_paused and self.queue.replaying.acquire(blocking=False)
        try:
            yield admitted
        finally:
            if admitted:
                self.queue.replaying.release()

    def apply_observation(self, row, project):
        with self.repository.try_access() as available:
            if not available or self.store.activity_paused:
                return False
            if not self.repository.has_receipt(row['capture_id']):
                result = project(self.store, row)
                self.repository.record_receipt(row, bool(result))
        return True
