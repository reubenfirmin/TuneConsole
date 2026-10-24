"""Prepared candidate upgrades and bounded detached current-listening samples.

The coordinator publishes these results only while their session and candidate
objects are still current. Preparation never writes a playlist or changes a plan.
"""
import time
from contextlib import contextmanager
from dataclasses import dataclass

from yt_playlist.core.activity import cancellable_work, check_stopping
from yt_playlist.core.work_diagnostics import work_span


@dataclass(frozen=True)
class CandidateUpgrade:
    candidates: object
    directions: object


class RadioPreparation:
    def __init__(self, store, policy, work, now_fn):
        self.store, self.policy, self.work, self.now_fn = store, policy, work, now_fn

    def upgrade(self, s, candidates_factory, worker=None):
        # The caller holds the operation lock. Pending choices and writes settle
        # before a fallback pool can be replaced by worker-published regions.
        if (s.stop.is_set() or not s.directions or s.candidates.modes
                or s.initial_tracks or s.pending or s.replacement or s.requested_direction
                or time.monotonic() - s.candidates_checked_at < 6):
            return None
        s.candidates_checked_at = time.monotonic()
        with self.store._lock:
            ready = bool(self.store.settings.get_setting('rec_content_model')
                         and self.store.rec_proposals.get_proposals('mode_bundles'))
        if not ready:
            if worker and not worker.busy and not s.preparation_requested:
                worker.trigger_mode_refresh()
                s.preparation_requested = True
            return None
        candidates = candidates_factory(self.store, self.now_fn(), s.candidates.tuning,
                                        now_source=s.candidates)
        if not candidates.modes:
            return None
        directions = self.policy.upgrade_directions(s.candidate_view(), s.history_view(), candidates,
                                                    force=s.feedback_dirty)
        return CandidateUpgrade(candidates, directions)

    @contextmanager
    def sample_now(self, s, candidates, now):
        """Keep the source snapshot open through the caller's guarded publication."""
        with (self.store.priority.background(check_stopping),
              cancellable_work(lambda: s.stop.is_set() or not self.work.accepting),
              work_span('radio: current listening'), self.store.read_snapshot() as snapshot):
            yield snapshot, candidates.sample_now(now, store=snapshot.store)
