"""Decoupled recommendation worker (spec §3).

Rec computation runs OFF the sync/request path here: a single background thread rebuilds the
taste vectors and materializes the heavy/slow surfaces (auto-playlists, outward discovery) into
rec_proposals for last-good serving. Triggers coalesce — many syncs in a row collapse into one
rebuild — so frequent syncs never pile up.
"""
import threading
import time

from yt_playlist import embed, recommend
from yt_playlist.rec_dao import RecDao


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
        from yt_playlist import discover
        while True:
            time.sleep(self.discovery_tick_s)
            try:
                discover.run_discovery(self.ctx, self.ctx.now_fn())
            except Exception:  # noqa: BLE001 - a scan failure must never crash the ticker
                self.ctx.logger.warning("discovery tick failed", exc_info=True)

    def _gc_loop(self):
        """Daily sweep that deletes generated playlists you never played. An initial pass runs soon
        after start so a daily-restarted app still collects them, then once per gc_tick_s."""
        from yt_playlist import executor
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
            time.sleep(self.gc_tick_s)

    @property
    def busy(self):
        """True while a rebuild is scheduled or running — drives the 'refreshing…' UI state."""
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

        Each surface is materialized INDEPENDENTLY: the album/fresh surfaces hit YouTube and can
        fail (network, rate-limit, parse), and a single failure must not block the surfaces after it
        from refreshing — otherwise e.g. a flaky album fetch would leave new-artist thumbnails stuck
        on stale cache forever. A failed surface keeps its last-good proposals."""
        from yt_playlist import discover
        log = self.ctx.logger
        store = self.ctx.store
        dao = RecDao(store)
        now = self.ctx.now_fn()
        t0 = time.monotonic()
        log.info("rec rebuild: starting")
        n = embed.build_and_store(store)
        log.info("rec rebuild: embedded %d vectors in %.1fs", n, time.monotonic() - t0)
        # Materialize deeper pools than a single card shows, so each surface has several epochs of
        # material to rotate through before it has to wrap.
        surfaces = (
            ("auto_playlists", lambda: recommend.auto_playlists(store, k=40)),
            ("fresh_songs", lambda: recommend.fresh_songs(self.ctx, limit=36)),
        )
        for surface, build in surfaces:
            ts = time.monotonic()
            try:
                items = build()
                dao.put_proposals(surface, items, now)
                log.info("rec rebuild: %s → %d items in %.1fs", surface, len(items), time.monotonic() - ts)
            except Exception:  # noqa: BLE001 - one surface's failure must not starve the others
                log.warning("rec rebuild: %s failed after %.1fs", surface, time.monotonic() - ts, exc_info=True)
        # Outward discovery (new albums + new artists) is now an accumulating, scan-ledger-backed pass
        # over ALL interested artists, a budgeted batch at a time — not a top-10 overwrite each sync.
        try:
            res = discover.run_discovery(self.ctx, now)
            log.info("rec rebuild: discovery scanned %d artists", res.get("scanned", 0))
        except Exception:  # noqa: BLE001
            log.warning("rec rebuild: discovery pass failed", exc_info=True)
        log.info("rec rebuild: done in %.1fs", time.monotonic() - t0)
