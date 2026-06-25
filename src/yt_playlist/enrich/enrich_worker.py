"""Background enrichment worker: drains the track corpus through the waterfall in priority order.

A single daemon loop pulls the next priority batch (store.next_enrich_batch), runs it through the
existing run_waterfall harness (which paces/rate-limits per provider), marks the tracks processed,
and repeats. When the primary queue empties it stamps `enrich_caught_up_at` (so later arrivals count
as "new" and jump the queue) and, if any processed track has gone stale while still incomplete, runs
a slow re-sweep; otherwise it idles until woken by trigger() or a timeout.

Mirrors RecWorker's shape (daemon thread off ctx, coalescing trigger, a `busy` flag). The drain step
is exposed synchronously as drain_once() so it can be tested without threads or network.
"""
import os
import threading

from yt_playlist.providers import enrichment
from yt_playlist.providers.waterfall import run_waterfall


class EnrichWorker:
    def __init__(self, ctx, idle_sleep_s=3.0, waterfall_fn=run_waterfall):
        self.ctx = ctx
        self.idle_sleep_s = idle_sleep_s
        self.waterfall_fn = waterfall_fn
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._busy = False
        self._had_work = False
        self._shutdown = False

    # --- settings (re-read each cycle so changes take effect on the next batch) ------------------
    def _enabled(self) -> bool:
        return self.ctx.store.get_setting("enrich_worker_enabled", "1") == "1"

    def _batch_size(self) -> int:
        try:
            return max(1, int(self.ctx.store.get_setting("enrich_batch_size", "40")))
        except (TypeError, ValueError):
            return 40

    def _resweep_days(self) -> float:
        try:
            return float(self.ctx.store.get_setting("enrich_resweep_days", "30"))
        except (TypeError, ValueError):
            return 30.0

    def _stop(self) -> bool:
        """should_stop for run_waterfall: abort the in-flight batch on pause or shutdown."""
        return self._shutdown or not self._enabled()

    @property
    def busy(self) -> bool:
        return self._busy

    # --- lifecycle ------------------------------------------------------------------------------
    def start_ticker(self):
        """Start the drain daemon (idempotent). Seeds enrich_caught_up_at on first run so the existing
        corpus counts as 'not new' (ordered by plays), only future arrivals jump the queue."""
        with self._lock:
            if self._started:
                return
            self._started = True
        # Don't launch the draining thread under pytest: it would call the real (network-hitting)
        # waterfall from the background. Tests drive drain_once() directly instead.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if not self.ctx.store.get_setting("enrich_caught_up_at"):
            self.ctx.store.set_setting("enrich_caught_up_at", str(self.ctx.now_fn()))
        threading.Thread(target=self._loop, daemon=True).start()

    def trigger(self):
        """Wake the drain loop (e.g. after a sync adds tracks). Coalesces: a no-op if already awake."""
        self._wake.set()

    def shutdown(self):
        self._shutdown = True
        self._wake.set()

    def _loop(self):
        while not self._shutdown:
            if not self._enabled():
                self._wake.wait(self.idle_sleep_s)
                self._wake.clear()
                continue
            try:
                n = self.drain_once()
            except Exception:  # noqa: BLE001 - a batch failure must never crash the worker
                self.ctx.logger.warning("enrich worker batch failed", exc_info=True)
                n = 0
            if n == 0:                       # caught up (or paused mid-batch). Idle until woken
                self._wake.wait(self.idle_sleep_s)
                self._wake.clear()

    # --- the drain step (synchronous; the unit tests call this directly) -------------------------
    def drain_once(self, limit=None) -> int:
        """Process one batch and return how many tracks it covered. Prefers the primary priority
        queue; when that's empty, stamps caught-up and falls back to a re-sweep batch. Returns 0 when
        there's nothing to do or the batch was aborted by a pause."""
        store = self.ctx.store
        limit = limit or self._batch_size()
        batch = store.next_enrich_batch(limit)
        if batch:
            self._had_work = True
        else:
            if self._had_work:               # just drained the primary queue. Mark the catch-up point
                store.set_setting("enrich_caught_up_at", str(self.ctx.now_fn()))
                self._had_work = False
            stale_before = self.ctx.now_fn() - self._resweep_days() * 86400
            batch = store.resweep_batch(limit, stale_before)
        if not batch:
            return 0
        self._busy = True
        try:
            self.waterfall_fn(store, batch, enrichment.load_config(store),
                              on_progress=lambda e: None, should_stop=self._stop)
        finally:
            self._busy = False
        if self._stop():                     # paused/shutting down mid-batch. Re-queue it next time
            return 0
        store.mark_enriched([t["id"] for t in batch], self.ctx.now_fn())
        try:                                  # keep the content (genre/era) cluster space current as
            from yt_playlist.rec import embed  # coverage grows; never let a rebuild crash the drain
            embed.maybe_rebuild_content_vectors(store)
        except Exception:  # noqa: BLE001
            self.ctx.logger.warning("content-vector rebuild after enrich batch failed", exc_info=True)
        return len(batch)
