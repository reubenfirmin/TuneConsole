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
from yt_playlist.core.activity import activity_for, WorkClosing


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
        self._activity = {}
        self._thread = None
        self._priority = {}
        self.work = activity_for(ctx)

    # --- settings (re-read each cycle so changes take effect on the next batch) ------------------
    def _enabled(self) -> bool:
        return self.ctx.store.settings.get_setting("enrich_worker_enabled", "1") == "1"

    def _batch_size(self) -> int:
        try:
            return max(1, int(self.ctx.store.settings.get_setting("enrich_batch_size", "40")))
        except (TypeError, ValueError):
            return 40

    def _resweep_days(self) -> float:
        try:
            return float(self.ctx.store.settings.get_setting("enrich_resweep_days", "30"))
        except (TypeError, ValueError):
            return 30.0

    def _stop(self) -> bool:
        """should_stop for run_waterfall: abort the in-flight batch on pause or shutdown."""
        return self._shutdown or not self._enabled()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def activity(self) -> dict:
        """A thread-safe snapshot for the live status panel; never persisted as a setting."""
        with self._lock:
            return dict(self._activity)

    def _record_activity(self, **values):
        with self._lock:
            self._activity.update(values, updated_at=self.ctx.now_fn())

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
        if not self.ctx.store.settings.get_setting("enrich_caught_up_at"):
            self.ctx.store.settings.set_setting("enrich_caught_up_at", str(self.ctx.now_fn()))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def trigger(self):
        """Wake the drain loop (e.g. after a sync adds tracks). Coalesces: a no-op if already awake."""
        self._wake.set()

    def prioritize(self, keys):
        """Bound and coalesce rendered radio requests; the one worker owns all lookups."""
        with self._lock:
            for key in keys:
                self._priority[key] = None
            while len(self._priority) > 96:
                self._priority.pop(next(iter(self._priority)))
        self.trigger()

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
        while not self._shutdown:
            if not self._enabled():
                self._wake.wait(self.idle_sleep_s)
                self._wake.clear()
                continue
            try:
                n = self.work.wrap("enrichment", self.drain_once)()
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
        store = self.ctx.store
        if self._stop():
            return 0
        limit = limit or self._batch_size()
        from yt_playlist.enrich import artwork
        self._busy = True
        try:
            art_completed = artwork.retry_due(store, self.ctx.now_fn, should_stop=self._stop,
                on_progress=lambda event: self._record_activity(warning=event["text"]))
        finally:
            self._busy = False
        scope = "queue"
        config = enrichment.load_config(store)
        registry = getattr(self.waterfall_fn, "keywords", {}).get("registry", REGISTRY)
        configuration = enrichment.provider_configuration(store, registry, config)
        store.enrichment.reconcile_provider_configuration(configuration)
        providers = [name for name, p in configuration.items() if p["status"] == "available"]
        with self._lock:
            priority = list(self._priority)
        stale_before = self.ctx.now_fn() - self._resweep_days() * 86400
        batch = store.enrichment.priority_enrich_batch(priority, min(limit, 24), stale_before,
            providers=providers, now=self.ctx.now_fn()) if priority else []
        if batch:
            scope = "radio"
        else:
            batch = store.enrichment.next_enrich_batch(limit, providers=providers, now=self.ctx.now_fn())
        if batch:
            self._had_work = True
        else:
            if self._had_work and not store.enrichment.queue_remaining():
                store.settings.set_setting("enrich_caught_up_at", str(self.ctx.now_fn()))
                self._had_work = False
            stale_before = self.ctx.now_fn() - self._resweep_days() * 86400
            batch = store.enrichment.resweep_batch(limit, stale_before, providers=providers, now=self.ctx.now_fn())
            scope = "refresh"
        if not batch:
            with self._lock:
                self._activity = ({**self._activity, "state": "waiting",
                                   "text": "Pending provider retries are deferred; other eligible work continues."}
                                  if store.enrichment.queue_remaining() else {})
            return art_completed
        if scope == "queue":
            for track in batch:
                if track.get("last_enriched_at") is not None:
                    track["provider_jobs"] = store.enrichment.enrichment_provider_jobs(track["recording_id"])
        completed = self._run_batch(batch, scope)
        if completed:
            with self._lock:
                for track in batch:
                    if track["job_id"] in completed:
                        self._priority.pop(track["identity_key"], None)
        if completed:
            try:                                  # metadata placement must never crash the drain
                keys = {t["identity_key"] for t in batch if t["job_id"] in completed}
                rec_worker = getattr(self.ctx, "rec_worker", None)
                if rec_worker is not None:
                    rec_worker.trigger_content_update(keys)
                else:
                    from yt_playlist.rec import content_updates
                    content_updates.update_tracks(store, keys, self.ctx.now_fn())
            except Exception:  # noqa: BLE001
                self.ctx.logger.warning("recommendation refresh after enrich batch failed", exc_info=True)
        return len(completed) + art_completed

    def _run_batch(self, batch, scope):
        """Commit each completed track, so a slow/failed/paused batch cannot hide or lose progress.

        A provider breaker may skip some tracks: only fully attempted events may leave the queue.
        The returned-id fallback keeps older injected waterfalls compatible.
        """
        completed = set()
        batch_keys = {t['job_id'] for t in batch}

        def mark(keys):
            self.ctx.store.enrichment.mark_recordings_enriched(keys, self.ctx.now_fn())

        def sink_for(track):
            return TrackSink(self.ctx.store, track)

        self._busy = True
        with self._lock:
            self._activity = {"state": "running", "scope": scope, "text": "Starting lookups…",
                              "updated_at": self.ctx.now_fn()}

        def progress(event):
            if event.get("warning"):
                self._record_activity(warning=event["text"])
                return
            if event.get("type") == "track" and event.get("fully_attempted"):
                track_key = event.get('job_id', event.get('track_id'))
                if track_key in batch_keys and track_key not in completed:
                    mark([track_key])
                    completed.add(track_key)
            state = ("blocked" if event.get("blocked") else
                     "waiting" if event.get("waiting") else "running")
            self._record_activity(state=state, text=event.get("text", ""))

        try:
            attempted = self.waterfall_fn(
                self.ctx.store, batch, enrichment.load_config(self.ctx.store),
                on_progress=progress, should_stop=self._stop, sink_for=sink_for,
                now_fn=self.ctx.now_fn, reuse_completed=True)
            if not self._stop():
                attempted = batch_keys if attempted is None else set(attempted) & batch_keys
                pending = attempted - completed
                mark(pending)
                completed.update(pending)
            return completed
        except Exception:
            self._record_activity(state="error", text="A lookup failed. Retrying shortly; see Network for request details.")
            raise
        finally:
            self._busy = False
