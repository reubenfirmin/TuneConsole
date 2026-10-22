"""Decoupled recommendation worker (spec §3).

Rec computation runs OFF the sync/request path here: a single background thread rebuilds the
taste vectors and materializes the heavy/slow surfaces (fresh songs, outward discovery) into
rec_proposals for last-good serving. Triggers coalesce: many syncs in a row collapse into one
rebuild, so frequent syncs never pile up.
"""
import threading
import time
import traceback

from yt_playlist.rec import actions, artist_model, embed, model_selection, surfaces
from yt_playlist.core.activity import activity_for, WorkClosing
from yt_playlist.core.work_diagnostics import work_span


class RecWorker:
    def __init__(self, ctx, debounce_s=2.0, discovery_tick_s=1800, gc_tick_s=86400, gc_initial_s=60,
                 *, enabled=True):
        self.ctx = ctx
        self.debounce_s = debounce_s
        self.discovery_tick_s = discovery_tick_s   # background discovery scan cadence (~30 min)
        self.gc_tick_s = gc_tick_s                 # generated-playlist GC cadence (daily)
        self.gc_initial_s = gc_initial_s           # first GC pass shortly after start (catches restarts)
        self._lock = threading.Lock()
        self.model_lock = threading.RLock()
        self._pending = False
        self._modes_pending = False
        self._ratings_pending = False
        self._content_pending = set()
        self._running = False
        self._ticker_started = False
        self.enabled = enabled
        self._stop = threading.Event()
        self._threads = []
        self.work = activity_for(ctx)
        model_selection.recover_interrupted(ctx.store)

    def start_ticker(self):
        """Start the periodic background daemons (idempotent): the budgeted discovery scan (so the
        album/artist pools keep filling between syncs) and the daily generated-playlist GC sweep."""
        with self._lock:
            if self._ticker_started or not self.enabled:
                return
            self._stop.clear()
            self._ticker_started = True
        for target in (self._tick_loop, self._gc_loop):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            self._pending = self._modes_pending = self._ratings_pending = False
            self._content_pending.clear()

    def join(self, timeout):
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("Recommendation worker did not stop")
        self._threads.clear()
        self._ticker_started = False

    def _tick_loop(self):
        from yt_playlist.rec import discover
        while not self._stop.wait(self.discovery_tick_s):
            try:
                with self.model_lock:
                    self.work.wrap("discovery", discover.run_discovery, self.ctx, self.ctx.now_fn())()
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a scan failure must never crash the ticker
                self.ctx.logger.warning("discovery tick failed", exc_info=True)

    def _gc_loop(self):
        """Daily sweep that deletes generated playlists you never played. An initial pass runs soon
        after start so a daily-restarted app still collects them, then once per gc_tick_s."""
        from yt_playlist.library import executor
        from yt_playlist.rec import discover
        if self._stop.wait(self.gc_initial_s):
            return
        while not self._stop.is_set():
            try:
                clients = self.ctx.client_provider() or {}
                if clients:        # no clients configured (e.g. pre-setup) -> nothing to delete remotely
                    collected = self.work.wrap("generated-playlist collection",
                        executor.gc_generated_playlists, self.ctx.store, clients, self.ctx.now_fn())()
                    if collected:
                        self.ctx.logger.info("GC: collected %d unplayed generated playlist(s): %s",
                                             len(collected), ", ".join(c["title"] for c in collected))
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a GC failure must never crash the daemon
                self.ctx.logger.warning("generated-playlist GC tick failed", exc_info=True)
            try:                   # #52: time-based discovery-pool GC (independent of remote clients)
                gc = self.work.wrap("discovery collection", discover.gc_discovery,
                                    self.ctx, self.ctx.now_fn())()
                if any(gc.values()):
                    self.ctx.logger.info("GC: discovery pool removed %s", gc)
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - discovery GC must never crash the daemon
                self.ctx.logger.warning("discovery-pool GC tick failed", exc_info=True)
            if self._stop.wait(self.gc_tick_s):
                return

    @property
    def busy(self):
        """True while a rebuild is scheduled or running, driving the 'refreshing…' UI state."""
        return self._running

    def trigger(self):
        """Request a rebuild. Coalesces: if one is already scheduled/running, just mark pending."""
        self._schedule(full=True)

    def trigger_mode_refresh(self):
        """Initialize missing genre groups and bundles; reuse an already prepared model."""
        self._schedule(full=False, modes=True)

    def trigger_rating_update(self):
        """Resolve exact-video rating subjects without blocking the bridge's response reader."""
        self._schedule(full=False, ratings=True)

    def trigger_content_update(self, keys=(), *, video_ids=()):
        """Coalesce changed recordings for placement into the existing model and cluster pools."""
        keys = set(keys)
        keys.update(key for vid in video_ids if (key := self.ctx.store.tracks.identity_key_for_video(vid)))
        if keys:
            self._schedule(full=False, keys=keys)

    def _schedule(self, *, full, modes=False, keys=(), ratings=False):
        with self._lock:
            if not self.enabled or self._stop.is_set() or not self.work.accepting:
                return
            previous = (self._pending, self._modes_pending, self._ratings_pending, len(self._content_pending))
            self._pending |= full
            self._modes_pending |= modes
            self._ratings_pending |= ratings
            self._content_pending.update(keys)
            if previous != (self._pending, self._modes_pending, self._ratings_pending, len(self._content_pending)):
                caller = traceback.extract_stack(limit=3)[0]
                self.ctx.logger.info("rec scheduled: kind=%s changed_tracks=%d running=%s source=%s:%d in %s",
                    "full" if full else ("ratings" if ratings else "initialization" if modes else "incremental"),
                    len(self._content_pending), self._running,
                    caller.filename, caller.lineno, caller.name)
            if self._running:
                return
            self._running = True
        self._start_loop()

    def _start_loop(self):
        try:
            run = self.work.wrap("recommendation updates", self._loop)
        except WorkClosing:
            self._running = False
            return
        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            run.release()
            self._running = False
            raise

    def _loop(self):
        while not self._stop.wait(self.debounce_s):
            with self._lock:
                if not (self._pending or self._modes_pending or self._content_pending or self._ratings_pending):
                    self._running = False
                    return
                full, modes, keys = self._pending, self._modes_pending, self._content_pending
                ratings_pending = self._ratings_pending
                self._pending = False
                self._modes_pending = False
                self._ratings_pending = False
                self._content_pending = set()
            if ratings_pending:
                try:
                    from yt_playlist.library import ratings
                    clients = self.ctx.client_provider() or {}
                    for identity in self.ctx.store.identities.get_identities():
                        keys.update(ratings.reconcile_pending(
                            self.ctx.store, identity.id, clients.get(identity.id)))
                except Exception:
                    self.ctx.logger.warning("rating subject update failed", exc_info=True)
            try:
                if full:
                    self._do_rebuild()           # includes genre groups and Home bundles
                elif modes:
                    self._do_mode_refresh()
            except Exception:  # noqa: BLE001 - never let rec work crash the app
                self.ctx.logger.warning("rec worker rebuild failed", exc_info=True)
                if full and modes:              # a failed full rebuild must not swallow local repair
                    self.trigger_mode_refresh()
            if keys:
                try:
                    from yt_playlist.rec import content_updates
                    with self.model_lock:
                        content_updates.update_tracks(self.ctx.store, keys, self.ctx.now_fn())
                except Exception:
                    self.ctx.logger.warning("rec incremental update failed: tracks=%d", len(keys), exc_info=True)
        self._running = False

    def rebuild(self):
        """Synchronous rebuild for the Taste-model 'rebuild' button. Guarded against the background
        loop: if a rebuild is already running it defers (marks pending) instead of starting a second
        concurrent SVD, and it flips `_running` so `busy` reflects a direct rebuild too."""
        with self._lock:
            if self._running:
                self._pending = True   # one's already in flight; let it absorb this request
                return
            self._running = True
        try:
            self.work.wrap("recommendation rebuild", self._do_rebuild)()
        finally:
            with self._lock:
                again = self._pending or self._modes_pending or self._ratings_pending or bool(self._content_pending)
                self._running = again
            if again:
                self._start_loop()

    def _stage(self, name, target, *args, **kwargs):
        with work_span("rec: " + name, announce=True):
            return target(*args, **kwargs)

    def _do_mode_refresh(self):
        with self.model_lock:
            self._refresh_modes()

    def _refresh_modes(self):
        """Initialize absent models/groups; ordinary startup reuses the prepared structures."""
        from yt_playlist.rec import mode_surfaces, taste_modes
        store = self.ctx.store
        if (store.settings.get_setting("rec_content_model") and not taste_modes.needs_refresh(store)
                and store.rec_surface.get_proposals("mode_bundles")):
            self.ctx.logger.info("rec initialization: reused saved model, clusters and candidate pools")
            return
        if not store.settings.get_setting("rec_content_model"):
            self._stage("initial content vectors", embed.maybe_rebuild_content_vectors, store)
        now = self.ctx.now_fn()
        if taste_modes.needs_refresh(store):
            self._stage("initial taste clusters", taste_modes.recompute, store, now)
        self._stage("cluster surfaces and PPR", mode_surfaces.prepare_bundles, store, now)
        self.ctx.logger.info("rec initialization: missing genre groups and Home bundles prepared")

    def _do_rebuild(self):
        # Source removal waits for publication, then clears every derived cache
        # before scheduling a build from the remaining evidence.
        with self.model_lock:
            self._rebuild()

    def _rebuild(self):
        """Rebuild vectors and materialize the heavy proposal surfaces.

        Surfaces go through a small table so adding more later stays uniform. Each is built in its own
        try/except: the build hits YouTube and can fail (network, rate-limit, parse), and a failure
        must leave that surface's last-good proposals in place rather than wiping its card to empty.
        Today the only such surface is Fresh songs; outward album discovery now accumulates separately
        in the discovery pool (see discover.pick_discovered_albums), refreshed by the discovery tick."""
        from yt_playlist.rec import discover
        log = self.ctx.logger
        store = self.ctx.store
        now = self.ctx.now_fn()
        t0 = time.monotonic()
        log.info("rec rebuild: starting")
        restored = False
        restore_id = store.settings.get_setting("rec_selection_restore")
        if restore_id:
            self._stage("model followup", model_selection.update_followup, store, now, now_fn=self.ctx.now_fn)
            restored = store.model_selection.restore_model_selection(int(restore_id), now)
            store.settings.set_setting("rec_selection_restore", "")
        n = store.rec_model.rec_vectors_count() if restored else self._stage("taste embedding", embed.build_and_store, store)
        log.info("rec rebuild: embedded %d vectors in %.1fs", n, time.monotonic() - t0)
        try:
            self._stage("model comparison", model_selection.maintain, store, now, now_fn=self.ctx.now_fn)
        except Exception:  # comparison failures must leave the regular rebuild usable
            log.warning("automatic model comparison failed", exc_info=True)
        # Keep the content (genre/era) space + its model in step with the collaborative rebuild, so the
        # cluster blend and the out-of-corpus "new music" pool refresh on the regular rebuild cadence
        # rather than ONLY when an enrichment batch happens to cross a coverage bucket (#48). Bucket-gated
        # inside (cheap when nothing changed; forced when the model is missing); guarded so a content
        # failure never breaks the rebuild.
        try:
            self._stage("content vectors", embed.maybe_rebuild_content_vectors, store)
        except Exception:  # noqa: BLE001 - content space is best-effort; don't fail the rebuild
            log.warning("rec rebuild: content-vector refresh failed", exc_info=True)
        # Taste modes (#60): cluster the freshly-rebuilt content space into the user's distinct taste
        # regions, with stable ids across recomputes. Read-only for now (nothing ranks on them yet);
        # guarded so a mode failure never breaks the rebuild.
        try:
            from yt_playlist.rec import taste_modes
            nm = self._stage("taste clusters", taste_modes.recompute, store, now)
            log.info("rec rebuild: taste modes -> %d active", nm)
        except Exception:  # noqa: BLE001 - modes are best-effort; don't fail the rebuild
            log.warning("rec rebuild: taste-modes recompute failed", exc_info=True)
        # Part B (#60): bucket every Home surface's pool by mode so the request can assemble distinct,
        # mode-focused cards without ranking anything live. Guarded; a failure leaves last-good bundles.
        try:
            from yt_playlist.rec import mode_surfaces
            self._stage("cluster surfaces and PPR", mode_surfaces.prepare_bundles, store, now)
            log.info("rec rebuild: mode bundles prepared")
        except Exception:  # noqa: BLE001 - mode bundles are best-effort; don't fail the rebuild
            log.warning("rec rebuild: mode-bundle prep failed", exc_info=True)
        # #76-#80 Trends rollups: first-play index + weekly/monthly rollup + spotlight candidate,
        # materialized for the /trends page and the intermittent Home spotlight. Guarded; a failure
        # leaves the last-good rollup in place and never breaks the rebuild.
        try:
            from yt_playlist.rec import trend_rollups
            self._stage("trend rollups", trend_rollups.build, store, now)
            log.info("rec rebuild: trend rollups built")
        except Exception:  # noqa: BLE001 - trend rollups are best-effort; don't fail the rebuild
            log.warning("rec rebuild: trend-rollup build failed", exc_info=True)
        # #28 artist-relationship model, built alongside the track embedding. Guarded: an artist-model
        # failure must never break the track rebuild (the model is consumed by nothing critical yet).
        try:
            ta = time.monotonic()
            na = self._stage("artist model", artist_model.build_artist_model_and_store, store)
            log.info("rec rebuild: artist model %d artists in %.1fs", na, time.monotonic() - ta)
        except Exception:  # noqa: BLE001 - artist model is best-effort; don't fail the rebuild
            log.warning("rec rebuild: artist model failed", exc_info=True)
        # Wikipedia 'into recently' cards: pre-fetch the whole fresh-subject pool so the Home card
        # serves from a warm cache instead of blocking on a live fetch when the rotation lands on a
        # new subject. Guarded + best-effort: a fetch failure leaves the prior cache in place. The
        # subject pool tracks the transient model, which we just rebuilt, so this is the right moment.
        try:
            from yt_playlist.rec import into_recently
            warmed = self._stage("wiki prewarm", into_recently.prewarm_pool, store, now)
            log.info("rec rebuild: warmed %d wiki card(s)", warmed)
        except Exception:  # noqa: BLE001 - wiki prewarm is best-effort; never fail the rebuild
            log.warning("rec rebuild: wiki prewarm failed", exc_info=True)
        # Playlist-cleanup summary: a local-only O(n²) scan we keep OFF the home request path by
        # materializing it here (sync changes the playlists this depends on, so a rebuild is the right
        # moment). Last-good: a failure leaves the previous summary in place.
        try:
            payload = self._stage("playlist cleanup", actions.refresh_cleanup, store, now)
            log.info("rec rebuild: cleanup → %d playlist(s)", payload["count"])
        except Exception:  # noqa: BLE001 - never let the cleanup scan crash a rebuild
            log.warning("rec rebuild: cleanup summary failed", exc_info=True)
        # Outward discovery (new albums + new artists) is now an accumulating, scan-ledger-backed pass
        # over ALL interested artists, a budgeted batch at a time, not a top-10 overwrite each sync.
        try:
            res = self._stage("discovery", discover.run_discovery, self.ctx, now)
            log.info("rec rebuild: discovery scanned %d artists", res.get("scanned", 0))
        except Exception:  # noqa: BLE001
            log.warning("rec rebuild: discovery pass failed", exc_info=True)
        log.info("rec rebuild: done in %.1fs", time.monotonic() - t0)
