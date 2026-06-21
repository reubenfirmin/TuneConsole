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
    def __init__(self, ctx, debounce_s=2.0):
        self.ctx = ctx
        self.debounce_s = debounce_s
        self._lock = threading.Lock()
        self._pending = False
        self._running = False

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
        surfaces = (
            ("auto_playlists", lambda: recommend.auto_playlists(store, k=40)),
            ("discover", lambda: recommend.new_albums_from_favorites(self.ctx)),
            ("fresh_songs", lambda: recommend.fresh_songs(self.ctx)),
            ("new_artists", lambda: discover.new_artists(self.ctx)),
        )
        for surface, build in surfaces:
            ts = time.monotonic()
            try:
                items = build()
                dao.put_proposals(surface, items, now)
                log.info("rec rebuild: %s → %d items in %.1fs", surface, len(items), time.monotonic() - ts)
            except Exception:  # noqa: BLE001 - one surface's failure must not starve the others
                log.warning("rec rebuild: %s failed after %.1fs", surface, time.monotonic() - ts, exc_info=True)
        log.info("rec rebuild: done in %.1fs", time.monotonic() - t0)
