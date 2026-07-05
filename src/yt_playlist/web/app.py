import hashlib
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from markupsafe import Markup, escape
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yt_playlist.core.bridge import Bridge
from yt_playlist.core import updatecheck
from yt_playlist.rec.radio import RadioSession
from yt_playlist.web.context import Ctx
from yt_playlist.web.jobs import SyncJobs
from yt_playlist.web.routes import build_all
from yt_playlist.web.routes.bridge import build as build_bridge_route


def _install_idle_shutdown(app, templates):
    """The packaged launchers (Flatpak / .app) set YT_PLAYLIST_EXIT_ON_IDLE=1. They run headless with
    no terminal to Ctrl-C and no window to quit, so the UI heartbeats the server and it shuts itself
    down once the tab is gone — otherwise it lingers on the port and blocks the next launch. Off by
    default (a `uv run` dev server keeps running until you Ctrl-C it)."""
    if os.environ.get("YT_PLAYLIST_EXIT_ON_IDLE") != "1":
        templates.env.globals["exit_on_idle"] = False
        return
    templates.env.globals["exit_on_idle"] = True
    st = {"last": time.monotonic(), "seen": False, "bye_at": None}

    @app.post("/heartbeat")
    def _heartbeat():
        st["last"] = time.monotonic()
        st["seen"] = True
        st["bye_at"] = None                    # a live beat cancels a pending goodbye (page reload)
        return Response(status_code=204)

    @app.post("/goodbye")
    def _goodbye():
        st["bye_at"] = time.monotonic()        # tab closing (sendBeacon); a reload will re-beat
        return Response(status_code=204)

    def _monitor():
        start = time.monotonic()
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            if not st["seen"]:
                if now - start > 45:           # browser never connected; do not linger forever
                    break
                continue
            bye = st["bye_at"]
            if bye is not None and st["last"] < bye and now - bye > 2.5:
                break                          # tab closed (pagehide beacon) and no reload within grace
            # Backstop for a close that never sent the beacon (crash/force-quit). MUST be well above the
            # browser's background-tab timer throttling: a hidden tab's heartbeat interval gets throttled
            # to ~once/minute, so a short timeout would kill the server under an open-but-backgrounded
            # tab. 150s tolerates that; a real close is handled promptly by the beacon above.
            if now - st["last"] > 150:
                break
        logging.getLogger("yt_playlist").info("UI idle — shutting down server")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_monitor, daemon=True).start()

# Methods that don't change state: exempt from the cross-origin guard below.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_URL_RE = re.compile(r"\(?\s*(https?://[^)\s]+)\s*\)?")


def _linkify(text):
    """Escape text and replace each (possibly parenthesised) URL with a small ↗ link, so bios keep
    their wording but lose the raw URLs (e.g. the Wikipedia/CC-BY-SA attribution stays clickable)."""
    if not text:
        return ""
    s, out, last = str(text), [], 0
    for m in _URL_RE.finditer(s):
        out.append(escape(s[last:m.start()]))
        url = m.group(1)
        out.append(Markup('<a href="{}" target="_blank" rel="noopener nofollow">↗</a>').format(url))
        last = m.end()
    out.append(escape(s[last:]))
    return Markup("").join(out)


# How often the background library-sync daemon wakes, and how stale the library may get before it
# re-pulls. The first tick fires POLL_S after start (so the initial post-setup sync lands shortly
# after the extension first connects, not instantly on boot).
_SYNC_POLL_S = 30.0
_SYNC_MAX_AGE_S = 86400.0    # 24 hours: full sync is expensive, so after the initial population we
                            # only re-pull the library once a day. Plays stay current via the live feed.


def _maybe_check_update(ctx):
    """Refresh the cached latest-release version (daily-gated inside check_latest). Never raises,
    so a failed check can't disturb the sync daemon."""
    try:
        updatecheck.check_latest(ctx.store, ctx.now_fn())
    except Exception:  # noqa: BLE001 - update check must never crash the daemon
        ctx.logger.warning("update check tick failed", exc_info=True)


def _background_sync_loop(ctx, setup, bridge, *, poll_s=_SYNC_POLL_S, max_age_s=_SYNC_MAX_AGE_S):
    """Keep the library fresh with no manual card: once configured and the extension is connected,
    run a full sync when we've never synced or the last full sync is older than max_age_s. Covers
    both the initial post-setup sync (fires as soon as the extension pairs) and periodic refresh.
    Guarded by ctx.sync_lock so it never overlaps a manual POST /sync (or itself)."""
    while True:
        time.sleep(poll_s)
        _maybe_check_update(ctx)
        try:
            if setup is not None and not setup.configured:
                continue
            if not (bridge is not None and bridge.connected):
                continue                       # no live extension -> nothing to reach YouTube with
            last = ctx.store.get_setting("last_sync_at")
            if last is not None and (ctx.now_fn() - float(last)) < max_age_s:
                continue                       # synced recently enough
            if not ctx.sync_lock.acquire(blocking=False):
                continue                       # a manual sync is already running; try next tick
            # Run through a job so the automatic sync streams to the dashboard's live console too
            # (not just manual POST /sync). The console discovers it via GET /sync/active.
            job = ctx.jobs.create(kind="library")
            try:
                clients = ctx.client_provider() or {}
                if not clients:
                    continue
                from yt_playlist.library import sync as sync_mod
                sync_mod.sync_all(ctx.store, clients, ctx.now_fn(),
                                  on_progress=job.events.append,
                                  on_auth_expired=ctx.flag_auth_expired,
                                  on_auth_ok=ctx.clear_auth_expired)
            finally:
                job.done = True
                ctx.sync_lock.release()
            if ctx.rec_worker:                 # fold the freshly-synced library into the taste model
                ctx.rec_worker.trigger()
            if ctx.enrich_worker:              # drain any new tracks through the enrichment waterfall
                ctx.enrich_worker.trigger()
        except Exception:  # noqa: BLE001 - a sync failure must never crash the daemon
            ctx.logger.warning("background library sync tick failed", exc_info=True)


def create_app(store, client_provider, *, now_fn=time.time,
               allowed_hosts=("localhost", "127.0.0.1"), setup=None,
               bridge=None) -> FastAPI:
    # setup: optional Runtime-like collaborator (.configured, .credentials_present, .apply_setup).
    # When None, the app is treated as already configured and the /setup wizard is inert. This
    # keeps the existing two-arg call sites (and their tests) working unchanged.
    # bridge: optional shared Bridge for the extension WebSocket route. When None, one is created
    # here (kept optional so existing call sites work unchanged). The route authenticates the
    # extension by its origin, so there is no token to thread through.
    if bridge is None:
        bridge = Bridge()
    web_dir = Path(__file__).parent
    static_dir = web_dir / "static"
    templates = Jinja2Templates(directory=str(web_dir / "templates"))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    allowed = set(allowed_hosts)
    # Cache-bust the front-end by a CONTENT hash across ALL top-level static JS/CSS so browsers always
    # fetch the current build after an edit (otherwise a stale cached file silently diverges). Content
    # (not mtime): Flatpak zeroes every packaged file's mtime for reproducible builds, so an mtime-based
    # version was constant ("0") across rebuilds and browsers kept running a stale app.js. Evaluated
    # LAZILY per render, memoized by (mtime, size) so a dev edit still busts without a restart while the
    # hash is only recomputed when a file actually changes.
    class _AssetVersion:
        _key = None
        _val = "0"

        def __str__(self):
            try:
                files = sorted((p for p in [*static_dir.glob("*.js"), *static_dir.glob("*.css"),
                                            static_dir / "favicon.svg"] if p.exists()), key=lambda p: p.name)
                key = tuple((p.name, int(p.stat().st_mtime), p.stat().st_size) for p in files)
                if key != self._key:
                    h = hashlib.md5()
                    for p in files:
                        h.update(p.read_bytes())
                    self._val, self._key = h.hexdigest()[:12], key
                return self._val
            except (OSError, ValueError):
                return "0"
    templates.env.globals["asset_v"] = _AssetVersion()
    templates.env.filters["linkify"] = _linkify

    @app.middleware("http")
    async def guard_state_changes(request: Request, call_next):
        # This UI runs on loopback but is reachable by any web page the user visits
        # (and by DNS-rebinding attacks). Its POSTs delete/merge real playlists, so
        # they must be protected against cross-site request forgery. Two checks, both
        # only on unsafe methods so reads stay simple:
        #   1. Host must be a known-local name -> blocks DNS rebinding (attacker's
        #      domain rebound to 127.0.0.1 still sends Host: evil.example).
        #   2. Origin/Referer, when the browser sends one, must be a local origin ->
        #      blocks a cross-site form POST from a malicious page.
        if request.method not in _SAFE_METHODS:
            host = (request.headers.get("host") or "").rsplit(":", 1)[0]
            if host and host not in allowed:
                return PlainTextResponse("invalid host", status_code=400)
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlsplit(origin).hostname not in allowed:
                return PlainTextResponse("cross-origin request blocked", status_code=403)
        return await call_next(request)

    from yt_playlist.providers import genres as genre_lib
    genre_lib.configure(store)                                 # load (and seed) the genre whitelist

    ctx = Ctx(store=store, client_provider=client_provider, now_fn=now_fn,
              templates=templates, jobs=SyncJobs(), setup=setup, bridge=bridge,
              radio=RadioSession())
    app.include_router(build_bridge_route(ctx))
    from yt_playlist.rec.rec_worker import RecWorker
    ctx.rec_worker = RecWorker(ctx)                            # decoupled rec computation
    ctx.rec_worker.start_ticker()                             # periodic background discovery scan
    from yt_playlist.enrich.enrich_worker import EnrichWorker
    ctx.enrich_worker = EnrichWorker(ctx)                      # drains the corpus through the waterfall
    ctx.enrich_worker.start_ticker()
    templates.env.globals["auth_expired"] = ctx.auth_expired   # same dict; mutated during sync
    # Background library-sync daemon: runs the full sync (setup + periodic) with no manual card, so
    # the initial post-setup sync fires as soon as the extension connects and the library then
    # refreshes on its own. It gates on bridge.connected, so in tests (no live WS) it never fires.
    threading.Thread(target=_background_sync_loop, args=(ctx, setup, bridge), daemon=True).start()
    app.state.ctx = ctx                                        # exposed for tests/introspection
    for router in build_all(ctx):
        app.include_router(router)

    _install_idle_shutdown(app, templates)
    return app
