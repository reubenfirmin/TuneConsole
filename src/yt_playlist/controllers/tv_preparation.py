"""Guarded TV preparation with explicit snapshot, publication and worker ports."""
from threading import RLock

from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded, WorkClosing


class TVPreparation:
    """Publish preparation outcomes through the installed storage owner."""
    def __init__(self, repository):
        self.repository = repository

    def state(self):
        return self.repository.state()

    def record(self, failed, now):
        self.repository.record(failed, now)

    def draw(self):
        return self.repository.draw()

    def record_draw(self, videos):
        self.repository.record_draw(videos)


class TVPoolPreparation:
    def __init__(self, *, snapshot, cached, requested, publish, record, prepare, expiry, tuning,
                 max_age, retry_seconds):
        self.snapshot, self.cached, self.requested = snapshot, cached, requested
        self.publish, self.record, self.prepare = publish, record, prepare
        self.expiry, self.tuning = expiry, tuning
        self.max_age, self.retry_seconds = max_age, retry_seconds
        self.complete = None
        self.lock = RLock()

    def due(self, now):
        if self.complete is None:
            return True
        guard, pool, at, expires, requested = self.complete
        return (not at <= now < expires or not 0 <= now - pool['at'] < self.max_age()
                or requested != self.requested() or self.cached() != pool or not guard.is_current())

    def step(self, client, now):
        with self.lock:
            check_cancellable_work()
            if not self.due(now):
                return False
            self.complete = None
            requested, previous = self.requested(), self.cached()
            pool = previous
            try:
                with self.snapshot() as snapshot:
                    expiry, tuning_left = self.expiry(snapshot.store, now), self.tuning(snapshot.store, now)

                    def publish(payload):
                        nonlocal pool
                        def write():
                            if self.cached() != previous:
                                raise WorkBudgetExceeded('TV output changed during preparation')
                            self.publish(payload, now)
                        published = snapshot.publish(write)
                        if published:
                            pool = payload
                        return published

                    more = self.prepare(snapshot.store, client, now, previous or {}, publish)
                if not more and pool is not None:
                    expires = min(pool.get('retry_at', pool['at'] + self.retry_seconds()), pool['at'] + self.max_age())
                    if expiry is not None:
                        expires = min(expires, expiry)
                    if tuning_left:
                        expires = min(expires, now + tuning_left)
                    self.complete = snapshot, pool, now, expires, requested
            except (WorkBudgetExceeded, WorkClosing):
                raise
            except Exception:
                self.record(True, now)
                raise
            self.record(False, now)
            return more or requested != self.requested()
