"""Recommendation job admission and coalescing. The caller holds its worker lock."""
from collections import deque
from dataclasses import dataclass
from enum import StrEnum


class Job(StrEnum):
    RATINGS = "ratings"
    PREPARE = "prepare"
    FULL = "full"
    MODES = "modes"
    BUNDLES = "bundles"
    TRENDS = "trends"
    CLEANUP = "cleanup"
    ANALYTICS = "analytics"
    CONTENT = "content"
    HOME = "home"
    TV = "tv"
    ARTWORK = "artwork"
    STORIES = "stories"
    MEASUREMENT = "measurement"
    EVIDENCE_STORAGE = "evidence_storage"
    CHOICES = "choices"
    GROUPS = "groups"
    MODEL_HEALTH = "model_health"


@dataclass
class Retry:
    dirty: bool = False
    failures: int = 0
    retry_at: float = 0
    attempted_at: float = float("-inf")
    deferrals: int = 0
    minimum_interval: float = 0

    def started(self, now):
        self.attempted_at = now
        self.retry_at = max(self.retry_at, now + self.minimum_interval)

    def failed(self, now, *, base=1, cap=30, exponent_cap=5):
        self.failures += 1
        backoff = min(cap, base * 2 ** min(self.failures - 1, exponent_cap))
        self.retry_at = max(self.attempted_at + self.minimum_interval, now + backoff)
        self.dirty = True
        return backoff

    def succeeded(self):
        self.failures = 0
        self.deferrals = 0
        self.retry_at = max(0, self.attempted_at + self.minimum_interval)
        self.dirty = False

    def deferred(self, now):
        """Source churn is retriable, but must not become a continuous calculation loop."""
        self.deferrals += 1
        self.retry_at = max(self.retry_at, self.attempted_at + self.minimum_interval,
                            now + min(60, 5 * 2 ** min(self.deferrals - 1, 4)))
        self.dirty = True


class PendingJobs:
    def __init__(self):
        self.pending = {}
        self._inflight_measurements = {}
        self.retry = {job: Retry() for job in Job}
        # A successful publication is not permission to refit immediately on the
        # next enrichment write. Admit at most one start per minute for each
        # expensive phase. Ratings and serving projections keep their own clocks.
        for job in (Job.FULL, Job.BUNDLES, Job.GROUPS):
            self.retry[job].minimum_interval = 60
        self.retry[Job.PREPARE].minimum_interval = 5
        self.retry[Job.TV].minimum_interval = 60

    def __bool__(self):
        return bool(self.pending)

    @property
    def visible(self):
        """Measurements, summaries and shadow groups do not show a model refresh."""
        return any(job not in {Job.MEASUREMENT, Job.EVIDENCE_STORAGE, Job.CHOICES, Job.ANALYTICS, Job.GROUPS, Job.TV, Job.MODEL_HEALTH} for job in self.pending)

    @property
    def measurements(self):
        return self.pending.get(Job.MEASUREMENT, ())

    def has(self, job: Job):
        return job in self.pending

    def merge(self, job: Job, payload=True):
        """Flags coalesce, content unions, artwork caps at 40, analytics caps at 64 offers."""
        if job == Job.MEASUREMENT:
            offers = (*self.pending.get(job, ()), *self._inflight_measurements.values())
            if sum(getattr(offer, "work_units", 1) for offer in offers) + getattr(payload, "work_units", 1) > 64:
                return False
            self.pending.setdefault(job, deque()).append(payload)
        elif job in {Job.ARTWORK, Job.CONTENT}:
            values = set(self.pending.get(job, ()))
            values.update(list(payload)[:40] if job == Job.ARTWORK else payload)
            if job == Job.ARTWORK:
                values = set(sorted(values)[:40])
            if values:
                self.pending[job] = values
        else:
            self.pending[job] = True
        return True

    def take_measurements(self, *, limit=None):
        """Detach a small FIFO batch; cancellation can still take every pinned reader."""
        if limit is None:
            return self.pending.pop(Job.MEASUREMENT, deque())
        offers = self.pending.get(Job.MEASUREMENT, deque())
        batch, units = [], 0
        while offers:
            size = getattr(offers[0], 'work_units', 1)
            if batch and units + size > limit:
                break
            batch.append(offers.popleft())
            units += size
        if not offers:
            self.pending.pop(Job.MEASUREMENT, None)
        self._inflight_measurements.update((id(offer), offer) for offer in batch)
        return batch

    def finish_measurements(self, batch, *, retry=()):
        """Release admission slots, or return admitted captures to the FIFO head."""
        for offer in batch:
            self._inflight_measurements.pop(id(offer), None)
        if retry:
            self.pending.setdefault(Job.MEASUREMENT, deque()).extendleft(reversed(retry))

    def drain(self):
        """Detach one ordered pass. Arrivals during execution belong to the next pass.

        Keep FULL and MODES intent together: FULL absorbs local repair on success,
        but a failed full pass must hand MODES back. Home also prepares stories.
        """
        batch = {}
        for job in Job:
            if job == Job.MEASUREMENT or job not in self.pending:
                continue
            payload = self.pending.pop(job)
            if job == Job.ARTWORK:
                selected = sorted(payload)[:3]
                remaining = payload - set(selected)
                if remaining:
                    self.pending[job] = remaining
                payload = selected
            batch[job] = payload
        if Job.HOME in batch:
            self.retry[Job.HOME].dirty = False
            batch.setdefault(Job.STORIES, True)
        return batch

    def cancel(self):
        offers = self.take_measurements()
        self.pending.clear()
        for state in self.retry.values():
            state.dirty = False
        return offers
