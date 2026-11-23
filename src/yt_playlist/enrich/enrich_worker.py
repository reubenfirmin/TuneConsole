"""Background enrichment worker: drains one queue of library and discovery tracks.

A single daemon loop processes the shared priority queue with run_waterfall, which paces/rate-limits
each provider and marks completed tracks. When the queue empties it stamps `enrich_caught_up_at` so
later arrivals count as "new", then re-sweeps stale, incomplete tracks or idles until woken.

Mirrors RecWorker's shape (daemon thread off ctx, coalescing trigger, a `busy` flag). The drain step
is exposed synchronously as drain_once() so it can be tested without threads or network.
"""
import threading

from yt_playlist.providers import enrichment
from yt_playlist.providers.waterfall import run_waterfall, TrackSink, REGISTRY
from yt_playlist.core.activity import activity_for, cancellable_work, check_stopping, WorkClosing
from yt_playlist.controllers.enrichment_drain import EnrichmentDrain, EnrichmentBatch


class EnrichWorker:
    def __init__(self, ctx, idle_sleep_s=3.0, waterfall_fn=run_waterfall):
        self.ctx = ctx
        self.idle_sleep_s = idle_sleep_s
        self.waterfall_fn = waterfall_fn
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._busy = False
        self._shutdown = False
        self._activity = {}
        self._thread = None
        self._priority = {}
        self._priority_revision = 0
        self.work = activity_for(ctx)
        from yt_playlist.enrich import artwork
        self.commands = EnrichmentDrain(ctx.store.settings, ctx.store.enrichment_queue,
            ctx.store.enrichment_reports, ctx.store.enrichment_state, now=self._now,
            configuration=lambda registry: enrichment.provider_configuration(ctx.store, registry),
            run_batch=self._run_batch,
            retry_artwork=lambda **kwargs: artwork.retry_due(ctx.store, self._now, **kwargs),
            refresh=self._refresh, logger=ctx.logger)

    def _now(self):
        return self.ctx.now_fn()

    # --- settings (re-read each cycle so changes take effect on the next batch) ------------------
    def _enabled(self) -> bool:
        return self.ctx.store.settings.get_setting("enrich_worker_enabled", "1") == "1"

    def _stop(self) -> bool:
        """should_stop for run_waterfall: abort the in-flight batch on pause or shutdown."""
        return (self._shutdown or not self._enabled()
                or getattr(self.ctx.store, '_recommendation_retirement_pending', False))

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def activity(self) -> dict:
        """A thread-safe snapshot for the live status panel; never persisted as a setting."""
        with self._lock:
            return dict(self._activity)

    # --- lifecycle ------------------------------------------------------------------------------
    def start_ticker(self):
        """Start the drain daemon (idempotent). Seeds enrich_caught_up_at on first run so the existing
        corpus counts as 'not new' (ordered by plays), only future arrivals jump the queue."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._shutdown = False
            self._wake.clear()
        self.ctx.store.enrichment_queue.save_caught_up_at(self._now(), initialize=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def trigger(self):
        """Wake the drain loop (e.g. after a sync adds tracks). Coalesces: a no-op if already awake."""
        self._wake.set()

    def prioritize(self, keys):
        """Put the current radio view first; yield bulk work at the next track boundary."""
        with self._lock:
            priority = list(dict.fromkeys([*keys, *self._priority]))[:96]
            if priority != list(self._priority):
                self._priority = dict.fromkeys(priority)
                self._priority_revision += 1
        self.trigger()

    def _batch_stopped(self, revision):
        if self._stop():
            return True
        with self._lock:
            return revision != self._priority_revision

    def shutdown(self):
        self._shutdown = True
        self._wake.set()

    def join(self, timeout):
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("Enrichment worker did not stop")
        self._thread = None
        self._started = False

    def _loop(self):
        def drain():
            # Translate SQLite interruption before releasing the admitted
            # activity: shutdown can close admission before setting our flag.
            with cancellable_work(lambda: self._shutdown):
                return self.drain_once()

        while not self._shutdown:
            if not self._enabled():
                self._wake.wait(self.idle_sleep_s)
                self._wake.clear()
                continue
            try:
                with self.ctx.store.priority.background(check_stopping):
                    n = self.work.wrap("enrichment", drain)()
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a batch failure must never crash the worker
                self.ctx.logger.warning("enrich worker batch failed", exc_info=True)
                n = 0
            if n == 0:                       # caught up (or paused mid-batch). Idle until woken
                self._wake.wait(self.idle_sleep_s)
                self._wake.clear()

    # --- the drain step (synchronous; the unit tests call this directly) -------------------------
    def drain_once(self, limit=None) -> int:
        """Process one batch from the shared queue, then re-sweep stale tracks once it is empty.
        Completed tracks remain stamped even if a later lookup is interrupted."""
        with self._lock:
            priority = list(self._priority)
            priority_revision = self._priority_revision
        self._busy = True
        try:
            count, completed = self.commands.drain(limit=limit, priority=priority,
                registry=getattr(self.waterfall_fn, 'keywords', {}).get('registry', REGISTRY),
                should_stop=lambda: self._batch_stopped(priority_revision), activity=self._update_activity)
            with self._lock:
                for key in completed:
                    self._priority.pop(key, None)
            return count
        finally:
            self._busy = False

    def _update_activity(self, clear=False, **values):
        with self._lock:
            if clear:
                self._activity = {}
            else:
                self._activity.update(values, updated_at=self._now())

    def _refresh(self, keys):
        worker = getattr(self.ctx, 'rec_worker', None)
        if worker:
            worker.trigger_content_update(keys)
        else:
            self.ctx.store.rec_refresh.enqueue()

    def _run_batch(self, batch, scope, should_stop, activity):
        commands = EnrichmentBatch(self.ctx.store.enrichment_queue,
            lambda: enrichment.load_config(self.ctx.store),
            lambda *args, **kwargs: self.waterfall_fn(self.ctx.store, *args, **kwargs),
            lambda track: TrackSink(self.ctx.store, track), now=self._now, stopped=self._stop)
        return commands.run(batch, scope, should_stop, activity)
