import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from markupsafe import Markup, escape
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from yt_playlist.web.context import Ctx
from yt_playlist.web.jobs import SyncJobs
from yt_playlist.web.routes import build_all

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


def create_app(store, client_provider, *, now_fn=time.time,
               allowed_hosts=("localhost", "127.0.0.1"), setup=None) -> FastAPI:
    # setup: optional Runtime-like collaborator (.configured, .credentials_present, .apply_setup).
    # When None, the app is treated as already configured and the /setup wizard is inert — this
    # keeps the existing two-arg call sites (and their tests) working unchanged.
    web_dir = Path(__file__).parent
    static_dir = web_dir / "static"
    templates = Jinja2Templates(directory=str(web_dir / "templates"))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    allowed = set(allowed_hosts)
    # Cache-bust app.js/app.css by the newest mtime so browsers always fetch the current build
    # after an edit (otherwise a stale cached app.js silently diverges from the templates).
    try:
        asset_v = str(int(max((static_dir / f).stat().st_mtime
                              for f in ("app.js", "app.css", "favicon.svg"))))
    except OSError:
        asset_v = "0"
    templates.env.globals["asset_v"] = asset_v
    templates.env.filters["linkify"] = _linkify

    @app.middleware("http")
    async def require_configured(request: Request, call_next):
        # Until an identity config exists, funnel every page to the setup wizard. /setup and
        # /static stay reachable so the wizard can load its own assets (Alpine).
        path = request.url.path
        if setup is not None and not setup.configured \
                and not (path.startswith("/setup") or path.startswith("/static")):
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)

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

    ctx = Ctx(store=store, client_provider=client_provider, now_fn=now_fn,
              templates=templates, jobs=SyncJobs(), setup=setup)
    for router in build_all(ctx):
        app.include_router(router)

    return app
