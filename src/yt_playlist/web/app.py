import asyncio
import hashlib
import logging
import os
import re
import signal
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from markupsafe import Markup, escape
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yt_playlist.web.icons import arrow_icon

from yt_playlist.core.bridge import Bridge
from yt_playlist.core import updatecheck
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.work_diagnostics import CpuDiagnostics
from yt_playlist.core.update_lifecycle import acquire_restart_locks
from yt_playlist.rec.journeys import JOURNEY_LABELS
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.context import Ctx
from yt_playlist.web.jobs import SyncJobs
from yt_playlist.web.provider_links import provider_search_url
from yt_playlist.web.track_links import track_url
from yt_playlist.web.request_priority import RequestPriorityMiddleware
from yt_playlist.web.routes import build_all
from yt_playlist.web.routes.bridge import build as build_bridge_route


_UI_STARTUP_GRACE_S = 120.0
_UI_CLOSE_GRACE_S = 30.0
_UI_HEARTBEAT_TIMEOUT_S = 300.0
BACKGROUND_WORK = True
_DRAIN_TIMEOUT_S = 30.0


class _UIPresence:
    """Track document leases: pagehide also fires during navigation and browser handoffs."""

    def __init__(self):
        self._startup_until = time.monotonic() + _UI_STARTUP_GRACE_S
        self._pages = {}
        self._lock = threading.Lock()

    def heartbeat(self, page):
        with self._lock:
            self._pages[page] = time.monotonic() + _UI_HEARTBEAT_TIMEOUT_S

    def goodbye(self, page):
        with self._lock:
            # Tokenless legacy pages and launcher heartbeats remain supported, but their
            # anonymous goodbye cannot reliably identify which document went away.
            if page and page in self._pages:
                self._pages[page] = time.monotonic() + _UI_CLOSE_GRACE_S

    def idle(self):
        with self._lock:
            now = time.monotonic()
            self._pages = {page: until for page, until in self._pages.items() if until > now}
            return now >= self._startup_until and not self._pages


def _install_idle_shutdown(app, templates):
    """The packaged launchers (Flatpak / .app) set YT_PLAYLIST_EXIT_ON_IDLE=1. They run headless with
    no terminal to Ctrl-C and no window to quit, so the UI heartbeats the server and it shuts itself
    down once the tab is gone — otherwise it lingers on the port and blocks the next launch. Off by
    default (a `uv run` dev server keeps running until you Ctrl-C it)."""
    if os.environ.get("YT_PLAYLIST_EXIT_ON_IDLE") != "1":
        templates.env.globals["exit_on_idle"] = False
        return
    templates.env.globals["exit_on_idle"] = True
    presence = _UIPresence()

    @app.post("/heartbeat")
    def _heartbeat(page: str = ""):
        presence.heartbeat(page)
        ctx = getattr(app.state, "ctx", None)
        if ctx is not None and hasattr(ctx, "work"):
            ctx.work.resume_idle()
        return Response(status_code=204)

    @app.post("/goodbye")
    def _goodbye(page: str = ""):
        presence.goodbye(page)
        return Response(status_code=204)

    def _monitor(stop):
        while not stop.wait(1.0):
            ctx = getattr(app.state, "ctx", None)
            # Always allow startup to settle, even after an initial page immediately navigates
            # away. Each document has its own lease, so one closing tab cannot evict another.
            # Missing beacons expire slowly enough to tolerate background timer throttling.
            if presence.idle():
                if ctx is not None:
                    acquired, _ = acquire_restart_locks(ctx, reason="idle")
                    if acquired is None:
                        continue
                    # A returning UI can cancel an idle drain while the last job is finishing.
                    # Release the shutdown guards if its new lease arrived during this check.
                    if not presence.idle() or ctx.work.accepting:
                        for held in acquired:
                            held.release()
                        continue
                break
        if stop.is_set():
            return
        logging.getLogger("yt_playlist").info("UI idle — shutting down server")
        os.kill(os.getpid(), signal.SIGTERM)

    return _monitor

# Methods that don't change state: exempt from the cross-origin guard below.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_URL_RE = re.compile(r"\(?\s*(https?://[^)\s]+)\s*\)?")


def _linkify(text):
    """Escape text and replace each URL with an accessible source link, so bios keep
    their wording but lose the raw URLs (e.g. the Wikipedia/CC-BY-SA attribution stays clickable)."""
    if not text:
        return ""
    s, out, last = str(text), [], 0
    for m in _URL_RE.finditer(s):
        out.append(escape(s[last:m.start()]))
        url = m.group(1)
        out.append(Markup('<a href="{}" target="_blank" rel="noopener nofollow" '
                          'aria-label="Open biography source" title="Open biography source">{}</a>')
                   .format(url, arrow_icon("external")))
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
    """Refresh the cached latest-release version (interval-gated inside check_latest). Never raises,
    so a failed check can't disturb the sync daemon."""
    try:
        updatecheck.check_latest(ctx.store, ctx.now_fn())
    except Exception:  # noqa: BLE001 - update check must never crash the daemon
        ctx.logger.warning("update check tick failed", exc_info=True)


def _background_sync_loop(ctx, setup, bridge, *, poll_s=_SYNC_POLL_S, max_age_s=_SYNC_MAX_AGE_S,
                          stop=None):
    """Refresh due resources and retry failures with persisted backoff.

    A new extension connection retries failures promptly. The shared sync lock
    keeps automatic work from overlapping a manual POST /sync.
    """
    stop = stop or threading.Event()
    last_connection = None
    while not stop.wait(poll_s):
        try:
            release = ctx.work.admit("automatic sync")
        except WorkClosing:
            return
        try:
            _maybe_check_update(ctx)
            if setup is not None and not setup.configured:
                continue
            if not (bridge is not None and bridge.connected):
                continue                       # no live extension -> nothing to reach YouTube with
            connection = getattr(bridge, "connection_id", None)
            retry_now = connection is not None and connection != last_connection
            from yt_playlist.library.sync_outcomes import sync_due
            if not sync_due(ctx.store, ctx.now_fn(), max_age_s, retry_now=retry_now):
                last_connection = connection
                continue                       # synced recently enough
            if not ctx.sync_lock.acquire(blocking=False):
                continue                       # a manual sync is already running; try next tick
            # Run through a job so the automatic sync streams to the dashboard's live console too
            # (not just manual POST /sync). The console discovers it via GET /sync/active.
            job = ctx.jobs.create(kind="library")
            try:
                from yt_playlist.web.jobs import run_library_sync
                with ctx.work.entered(release):
                    last_connection = connection
                    run_library_sync(ctx, job, automatic=True, retry_now=retry_now, max_age_s=max_age_s)
            finally:
                ctx.sync_lock.release()
        except Exception:  # noqa: BLE001 - a sync failure must never crash the daemon
            ctx.logger.warning("background library sync tick failed", exc_info=True)
        finally:
            release()


def create_app(store, client_provider, *, now_fn=time.time,
               allowed_hosts=("localhost", "127.0.0.1"), setup=None,
               bridge=None, updater=None, background_work=None) -> FastAPI:
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
    background_work = BACKGROUND_WORK if background_work is None else background_work
    stop = threading.Event()
    @asynccontextmanager
    async def lifespan(app):
        ctx.work.open()
        stop.clear()
        ctx.work.start_executor()
        threads = []
        diagnostics = CpuDiagnostics(ctx.work)
        try:
            if background_work:
                diagnostics.start()
                ctx.radio.prepare()
                ctx.rec_worker.start_ticker()
                ctx.enrich_worker.start_ticker()
                ctx.rec_worker.trigger_mode_refresh()
                threads.append(threading.Thread(target=_background_sync_loop,
                    args=(ctx, setup, bridge), kwargs={"stop": stop}, daemon=True))
            if idle_monitor is not None:
                threads.append(threading.Thread(target=idle_monitor, args=(stop,), daemon=True))
            for thread in threads:
                thread.start()
            yield
        finally:
            ctx.work.close_admission()
            await ctx.pending_restart.close()
            diagnostics.stop()
            stop.set()
            ctx.rec_worker.shutdown()
            ctx.enrich_worker.shutdown()
            ctx.work.stop_executor()  # cancel queued applies; running mutations drain normally
            drained = await asyncio.to_thread(ctx.work.wait, _DRAIN_TIMEOUT_S)
            for thread in threads:
                await asyncio.to_thread(thread.join, 1.0)
            await asyncio.to_thread(ctx.rec_worker.join, 1.0)
            await asyncio.to_thread(ctx.enrich_worker.join, 1.0)
            if not drained or any(thread.is_alive() for thread in threads):
                raise RuntimeError(f"Application shutdown timed out: {ctx.work.active}")
            if ctx.updater is not None:
                await ctx.updater.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RequestPriorityMiddleware, priority=store.priority)
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
    templates.env.globals["arrow_icon"] = arrow_icon
    templates.env.globals["journey_labels"] = JOURNEY_LABELS
    templates.env.filters["linkify"] = _linkify
    templates.env.globals["provider_search_url"] = provider_search_url
    templates.env.globals["track_url"] = track_url

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
            ctx = getattr(app.state, "ctx", None)
            if (ctx is not None and ctx.updater is not None
                    and ctx.updater.phase == "restarting"
                    and request.url.path not in ("/app-update/restart", "/app-update/restart/cancel", "/heartbeat", "/goodbye")):
                return PlainTextResponse("TuneConsole is restarting. Please wait.", status_code=503)
        ctx = getattr(app.state, "ctx", None)
        release = None
        if (ctx is not None and request.method not in _SAFE_METHODS
                and request.url.path not in ("/app-update/restart", "/app-update/restart/cancel", "/heartbeat", "/goodbye")):
            try:
                release = ctx.work.admit("request " + request.url.path)
            except WorkClosing as error:
                return PlainTextResponse(str(error), status_code=503)
        try:
            if release is not None:
                with ctx.work.entered(release):
                    return await call_next(request)
            return await call_next(request)
        finally:
            if release is not None:
                release()

    @app.exception_handler(WorkClosing)
    async def closing_work(request, error):
        return PlainTextResponse(str(error), status_code=503)

    from yt_playlist.providers import genres as genre_lib
    genre_lib.configure(store)                                 # load (and seed) the genre whitelist

    ctx = Ctx(store=store, client_provider=client_provider, now_fn=now_fn,
              templates=templates, jobs=SyncJobs(), setup=setup, bridge=bridge)
    ctx.radio = PlaylistRadio(ctx)
    if updater is None and updatecheck.install_kind() == "flatpak":
        from yt_playlist.core.flatpak_update import FlatpakUpdater
        updater = FlatpakUpdater()
    ctx.updater = updater
    app.include_router(build_bridge_route(ctx))
    from yt_playlist.rec.rec_worker import RecWorker
    ctx.rec_worker = RecWorker(ctx, enabled=background_work)
    from yt_playlist.enrich.enrich_worker import EnrichWorker
    ctx.enrich_worker = EnrichWorker(ctx)                      # drains the corpus through the waterfall
    templates.env.globals["auth_expired"] = ctx.auth_expired   # same dict; mutated during sync
    from yt_playlist.rec import onboarding
    templates.env.globals["initial_setup_pending"] = lambda: onboarding.initial_setup_pending(store, now_fn())
    templates.env.globals["has_multiple_identities"] = (
        lambda: len(store.identities.get_identities()) > 1
    )
    # Background library-sync daemon: runs the full sync (setup + periodic) with no manual card, so
    # the initial post-setup sync fires as soon as the extension connects and the library then
    # refreshes on its own. It gates on bridge.connected, so in tests (no live WS) it never fires.
    app.state.ctx = ctx                                        # exposed for tests/introspection
    for router in build_all(ctx):
        app.include_router(router)

    idle_monitor = _install_idle_shutdown(app, templates)
    return app
