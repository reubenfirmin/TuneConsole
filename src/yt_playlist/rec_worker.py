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
                self.rebuild()
            except Exception:  # noqa: BLE001 - never let rec work crash the app
                self.ctx.logger.warning("rec worker rebuild failed", exc_info=True)

    def rebuild(self):
        """Rebuild vectors and materialize the heavy proposal surfaces."""
        store = self.ctx.store
        dao = RecDao(store)
        now = self.ctx.now_fn()
        embed.build_and_store(store)
        dao.put_proposals("auto_playlists", recommend.auto_playlists(store, k=24), now)
        dao.put_proposals("discover", recommend.new_albums_from_favorites(self.ctx), now)
        dao.put_proposals("fresh_songs", recommend.fresh_songs(self.ctx), now)
