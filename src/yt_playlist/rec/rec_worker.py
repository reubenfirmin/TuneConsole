"""Decoupled recommendation worker (spec §3).

Rec computation runs OFF the sync/request path here: a single background thread rebuilds the
taste vectors and materializes the heavy/slow surfaces (fresh songs, outward discovery) into
rec_proposals for last-good serving. Triggers coalesce: many syncs in a row collapse into one
rebuild, so frequent syncs never pile up.
"""
import threading
import time

from yt_playlist.rec import artist_model, embed, recommend, surfaces
from yt_playlist.rec.rec_dao import RecDao


class RecWorker:
    def __init__(self, ctx, debounce_s=2.0, discovery_tick_s=1800, gc_tick_s=86400, gc_initial_s=60):
        self.ctx = ctx
        self.debounce_s = debounce_s
        self.discovery_tick_s = discovery_tick_s   # background discovery scan cadence (~30 min)
        self.gc_tick_s = gc_tick_s                 # generated-playlist GC cadence (daily)
        self.gc_initial_s = gc_initial_s           # first GC pass shortly after start (catches restarts)
        self._lock = threading.Lock()
        self._pending = False
        self._running = False
        self._ticker_started = False

    def start_ticker(self):
        """Start the periodic background daemons (idempotent): the budgeted discovery scan (so the
        album/artist pools keep filling between syncs) and the daily generated-playlist GC sweep."""
        with self._lock:
            if self._ticker_started:
                return
            self._ticker_started = True
        threading.Thread(target=self._tick_loop, daemon=True).start()
        threading.Thread(target=self._gc_loop, daemon=True).start()

    def _tick_loop(self):
        from yt_playlist.rec import discover
        while True:
            time.sleep(self.discovery_tick_s)
            try:
                discover.run_discovery(self.ctx, self.ctx.now_fn())
            except Exception:  # noqa: BLE001 - a scan failure must never crash the ticker
                self.ctx.logger.warning("discovery tick failed", exc_info=True)

    def _gc_loop(self):
        """Daily sweep that deletes generated playlists you never played. An initial pass runs soon
        after start so a daily-restarted app still collects them, then once per gc_tick_s."""
        from yt_playlist.library import executor
        from yt_playlist.rec import discover
        time.sleep(self.gc_initial_s)
        while True:
            try:
                clients = self.ctx.client_provider() or {}
                if clients:        # no clients configured (e.g. pre-setup) -> nothing to delete remotely
                    collected = executor.gc_generated_playlists(self.ctx.store, clients, self.ctx.now_fn())
                    if collected:
                        self.ctx.logger.info("GC: collected %d unplayed generated playlist(s): %s",
                                             len(collected), ", ".join(c["title"] for c in collected))
            except Exception:  # noqa: BLE001 - a GC failure must never crash the daemon
                self.ctx.logger.warning("generated-playlist GC tick failed", exc_info=True)
            try:                   # #52: time-based discovery-pool GC (independent of remote clients)
                gc = discover.gc_discovery(self.ctx, self.ctx.now_fn())
                if any(gc.values()):
                    self.ctx.logger.info("GC: discovery pool removed %s", gc)
            except Exception:  # noqa: BLE001 - discovery GC must never crash the daemon
                self.ctx.logger.warning("discovery-pool GC tick failed", exc_info=True)
            time.sleep(self.gc_tick_s)

    @property
    def busy(self):
        """True while a rebuild is scheduled or running, driving the 'refreshing…' UI state."""
        return self._running

    def trigger(self):
        """Request a rebuild. Coalesces: if one is already scheduled/running, just mark pending."""
        with self._lock:
            self._pending = True
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(self.debounce_s)          # let a burst of triggers settle
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                self._pending = False
            try:
                self._do_rebuild()
            except Exception:  # noqa: BLE001 - never let rec work crash the app
                self.ctx.logger.warning("rec worker rebuild failed", exc_info=True)

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
            self._do_rebuild()
        finally:
            with self._lock:
                self._running = False
                again = self._pending
                self._pending = False
        if again:
            self.trigger()             # a request arrived mid-rebuild -> background catch-up pass

    def _do_rebuild(self):
        """Rebuild vectors and materialize the heavy proposal surfaces.

        Surfaces go through a small table so adding more later stays uniform. Each is built in its own
        try/except: the build hits YouTube and can fail (network, rate-limit, parse), and a failure
        must leave that surface's last-good proposals in place rather than wiping its card to empty.
        Today the only such surface is Fresh songs; outward album discovery now accumulates separately
        in the discovery pool (see discover.pick_discovered_albums), refreshed by the discovery tick."""
        from yt_playlist.rec import discover
        log = self.ctx.logger
        store = self.ctx.store
        dao = RecDao(store)
        now = self.ctx.now_fn()
        t0 = time.monotonic()
        log.info("rec rebuild: starting")
        n = embed.build_and_store(store)
        log.info("rec rebuild: embedded %d vectors in %.1fs", n, time.monotonic() - t0)
        # Keep the content (genre/era) space + its model in step with the collaborative rebuild, so the
        # cluster blend and the out-of-corpus "new music" pool refresh on the regular rebuild cadence
        # rather than ONLY when an enrichment batch happens to cross a coverage bucket (#48). Bucket-gated
        # inside (cheap when nothing changed; forced when the model is missing); guarded so a content
        # failure never breaks the rebuild.
        try:
            embed.maybe_rebuild_content_vectors(store)
        except Exception:  # noqa: BLE001 - content space is best-effort; don't fail the rebuild
            log.warning("rec rebuild: content-vector refresh failed", exc_info=True)
        # Taste modes (#60): cluster the freshly-rebuilt content space into the user's distinct taste
        # regions, with stable ids across recomputes. Read-only for now (nothing ranks on them yet);
        # guarded so a mode failure never breaks the rebuild.
        try:
            from yt_playlist.rec import taste_modes
            nm = taste_modes.recompute(store, now)
            log.info("rec rebuild: taste modes -> %d active", nm)
        except Exception:  # noqa: BLE001 - modes are best-effort; don't fail the rebuild
            log.warning("rec rebuild: taste-modes recompute failed", exc_info=True)
        # Part B (#60): bucket every Home surface's pool by mode so the request can assemble distinct,
        # mode-focused cards without ranking anything live. Guarded; a failure leaves last-good bundles.
        try:
            from yt_playlist.rec import mode_surfaces
            mode_surfaces.prepare_bundles(store, now)
            log.info("rec rebuild: mode bundles prepared")
        except Exception:  # noqa: BLE001 - mode bundles are best-effort; don't fail the rebuild
            log.warning("rec rebuild: mode-bundle prep failed", exc_info=True)
        # #76-#80 Trends rollups: first-play index + weekly/monthly rollup + spotlight candidate,
        # materialized for the /trends page and the intermittent Home spotlight. Guarded; a failure
        # leaves the last-good rollup in place and never breaks the rebuild.
        try:
            from yt_playlist.rec import trend_rollups
            trend_rollups.build(store, now)
            log.info("rec rebuild: trend rollups built")
        except Exception:  # noqa: BLE001 - trend rollups are best-effort; don't fail the rebuild
            log.warning("rec rebuild: trend-rollup build failed", exc_info=True)
        # #57 SHADOW: per-mode PPR (random walk) computed in parallel and appended to a persistent log
        # alongside the cosine ranking, for a non-circular comparison in ~2 weeks. Serves nothing.
        try:
            from yt_playlist.rec import ppr
            nm = ppr.shadow_log(store, now)
            log.info("rec rebuild: PPR shadow logged %d modes", nm)
        except Exception:  # noqa: BLE001 - shadow experiment must never break the rebuild
            log.warning("rec rebuild: PPR shadow log failed", exc_info=True)
        # #28 artist-relationship model, built alongside the track embedding. Guarded: an artist-model
        # failure must never break the track rebuild (the model is consumed by nothing critical yet).
        try:
            ta = time.monotonic()
            na = artist_model.build_artist_model_and_store(store)
            log.info("rec rebuild: artist model %d artists in %.1fs", na, time.monotonic() - ta)
        except Exception:  # noqa: BLE001 - artist model is best-effort; don't fail the rebuild
            log.warning("rec rebuild: artist model failed", exc_info=True)
        # Wikipedia 'into recently' cards: pre-fetch the whole fresh-subject pool so the Home card
        # serves from a warm cache instead of blocking on a live fetch when the rotation lands on a
        # new subject. Guarded + best-effort: a fetch failure leaves the prior cache in place. The
        # subject pool tracks the transient model, which we just rebuilt, so this is the right moment.
        try:
            from yt_playlist.rec import into_recently
            warmed = into_recently.prewarm_pool(store, now)
            log.info("rec rebuild: warmed %d wiki card(s)", warmed)
        except Exception:  # noqa: BLE001 - wiki prewarm is best-effort; never fail the rebuild
            log.warning("rec rebuild: wiki prewarm failed", exc_info=True)
        # Playlist-cleanup summary: a local-only O(n²) scan we keep OFF the home request path by
        # materializing it here (sync changes the playlists this depends on, so a rebuild is the right
        # moment). Last-good: a failure leaves the previous summary in place.
        try:
            payload = recommend.refresh_cleanup(store, now)
            log.info("rec rebuild: cleanup → %d playlist(s)", payload["count"])
        except Exception:  # noqa: BLE001 - never let the cleanup scan crash a rebuild
            log.warning("rec rebuild: cleanup summary failed", exc_info=True)
        # Outward discovery (new albums + new artists) is now an accumulating, scan-ledger-backed pass
        # over ALL interested artists, a budgeted batch at a time, not a top-10 overwrite each sync.
        try:
            res = discover.run_discovery(self.ctx, now)
            log.info("rec rebuild: discovery scanned %d artists", res.get("scanned", 0))
        except Exception:  # noqa: BLE001
            log.warning("rec rebuild: discovery pass failed", exc_info=True)
        log.info("rec rebuild: done in %.1fs", time.monotonic() - t0)
