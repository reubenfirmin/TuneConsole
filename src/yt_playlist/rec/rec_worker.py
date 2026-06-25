"""Decoupled recommendation worker (spec §3).

Rec computation runs OFF the sync/request path here: a single background thread rebuilds the
taste vectors and materializes the heavy/slow surfaces (fresh songs, outward discovery) into
rec_proposals for last-good serving. Triggers coalesce — many syncs in a row collapse into one
rebuild — so frequent syncs never pile up.
"""
import threading
import time

from yt_playlist.rec import embed, recommend
from yt_playlist.rec.rec_dao import RecDao


class RecWorker:
    def __init__(self, ctx, debounce_s=2.0, discovery_tick_s=1800, gc_tick_s=86400, gc_initial_s=60,
                 auto_sync_tick_s=1800, auto_sync_poll_s=60):
        self.ctx = ctx
        self.debounce_s = debounce_s
        self.discovery_tick_s = discovery_tick_s   # background discovery scan cadence (~30 min)
        self.gc_tick_s = gc_tick_s                 # generated-playlist GC cadence (daily)
        self.gc_initial_s = gc_initial_s           # first GC pass shortly after start (catches restarts)
        self.auto_sync_tick_s = auto_sync_tick_s   # auto-sync-plays cadence when the user opts in (~30 min)
        self.auto_sync_poll_s = auto_sync_poll_s   # how often the loop checks whether a sync is DUE
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
        threading.Thread(target=self._auto_sync_loop, daemon=True).start()

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

    def _auto_sync_due(self, now) -> bool:
        """Whether an auto-sync of plays should run NOW: the user has opted in (settings key
        `auto_sync_plays`), an account is connected, and it's been at least one tick since the last
        plays sync (or there's never been one). Due-based on the stored stamp - NOT a fixed sleep - so
        an app restart can't postpone it: on restart, if it's overdue, the next poll runs it."""
        if self.ctx.store.get_setting("auto_sync_plays") != "1":
            return False
        if not (self.ctx.client_provider() or {}):     # no account connected yet -> nothing to pull
            return False
        last = self.ctx.store.get_setting("last_plays_sync_at")
        return last is None or (now - float(last)) >= self.auto_sync_tick_s

    def _auto_sync_loop(self):
        """Keeps plays/likes current without a manual sync when auto-sync is on. Polls every
        auto_sync_poll_s and runs a sync whenever one is DUE (see _auto_sync_due). Polling on a short
        interval (rather than sleeping a full tick first) means restarting the app no longer pushes the
        next sync 30 minutes out - it catches up within a poll of being overdue, then holds the cadence."""
        from yt_playlist.library import sync as sync_mod
        while True:
            time.sleep(self.auto_sync_poll_s)
            try:
                if not self._auto_sync_due(self.ctx.now_fn()):
                    continue
                sync_mod.sync_plays_all(
                    self.ctx.store, self.ctx.client_provider() or {}, self.ctx.now_fn(),
                    on_auth_expired=lambda iid, label: self.ctx.auth_expired.__setitem__(iid, label or str(iid)),
                    on_auth_ok=lambda iid: self.ctx.auth_expired.pop(iid, None))
                self.trigger()         # fold the new plays/likes into the taste model (debounced)
            except Exception:  # noqa: BLE001 - an auto-sync failure must never crash the daemon
                self.ctx.logger.warning("auto-sync-plays tick failed", exc_info=True)

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
        from yt_playlist.rec import discover
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
        # Playlist-cleanup summary: a local-only O(n²) scan we keep OFF the home request path by
        # materializing it here (sync changes the playlists this depends on, so a rebuild is the right
        # moment). Last-good: a failure leaves the previous summary in place.
        try:
            payload = recommend.refresh_cleanup(store, now)
            log.info("rec rebuild: cleanup → %d playlist(s)", payload["count"])
        except Exception:  # noqa: BLE001 - never let the cleanup scan crash a rebuild
            log.warning("rec rebuild: cleanup summary failed", exc_info=True)
        # Outward discovery (new albums + new artists) is now an accumulating, scan-ledger-backed pass
        # over ALL interested artists, a budgeted batch at a time — not a top-10 overwrite each sync.
        try:
            res = discover.run_discovery(self.ctx, now)
            log.info("rec rebuild: discovery scanned %d artists", res.get("scanned", 0))
        except Exception:  # noqa: BLE001
            log.warning("rec rebuild: discovery pass failed", exc_info=True)
        log.info("rec rebuild: done in %.1fs", time.monotonic() - t0)
