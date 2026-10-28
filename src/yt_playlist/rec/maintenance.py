"""Coalesced, restart-safe scheduling for disposable listening/library summaries."""
import threading
import time
from typing import Callable

from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec.summary_inputs import DEPENDENCIES, SURFACES, input_fingerprints, inputs


class Projections:
    def __init__(self, store, now_fn, logger, *, accepting: Callable[[], bool],
                 schedule: Callable[[dict[str, bool]], bool], execute: Callable):
        self.store, self.now_fn, self.logger = store, now_fn, logger
        self.accepting, self.schedule, self.execute = accepting, schedule, execute
        self.lock = threading.Lock()
        self.completed = {}
        self.active = set()
        self.retry_at = {}
        self.failures = {}
        self.requested = set()

    def request(self, kind):
        with self.lock:
            self.requested.add(kind)
        self.poll(kinds=(kind,))

    def failed(self, kind):
        with self.lock:
            return bool(self.failures.get(kind))

    def poll(self, *, kinds=None):
        if not self.accepting():
            return
        pending = {}
        from yt_playlist.core.features import listening_charts_enabled
        if kinds is None:
            kinds = tuple(kind for kind in SURFACES
                          if (kind != 'analytics' or listening_charts_enabled())
                          and (kind != 'model_health' or kind in self.requested))
        for kind, fingerprint in input_fingerprints(self.store, kinds, self.now_fn()).items():
            with self.lock:
                if (kind not in self.active and fingerprint != self.completed.get(kind)
                        and time.monotonic() >= self.retry_at.get(kind, 0)):
                    self.active.add(kind)
                    pending[kind] = True
        if pending:
            if not self.schedule(pending):
                with self.lock:
                    self.active.difference_update(pending)

    def run(self, kind):
        from yt_playlist.rec import actions, listening_analytics, model_health, trend_rollups
        result = None
        try:
            fingerprint = inputs(self.store, kind, self.now_fn())
            target = {'trends': trend_rollups.build, 'cleanup': actions.refresh_cleanup,
                      'analytics': listening_analytics.build, 'model_health': model_health.prepare}[kind]
            result = self.execute(kind + ' summary', target, self.store, self.now_fn())
            return result
        except WorkClosing:
            raise
        except Exception:
            self.logger.warning('%s summary failed; retaining last-good result', kind, exc_info=True)
        finally:
            with self.lock:
                self.active.discard(kind)
                if result is None:
                    failures = self.failures.get(kind, 0) + 1
                    self.failures[kind] = failures
                    self.retry_at[kind] = time.monotonic() + min(300, 2 ** min(failures, 9))
                else:
                    # Publication creates the previously missing proposal. Source
                    # revisions remain those captured before calculation, so a
                    # concurrent edit cannot be marked clean accidentally.
                    self.completed[kind] = (*fingerprint[:-1], True)
                    self.failures[kind] = 0
                    self.retry_at[kind] = time.monotonic() + (120 if kind == 'analytics' else 30)
