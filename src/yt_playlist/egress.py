"""
==============================================================================
  EGRESS GUARD — every outbound network request the SERVER makes is gated here
==============================================================================

WHY THIS CLASS EXISTS (read this before changing anything)
----------------------------------------------------------
This app holds your Google sign-in cookies (see the notes on /setup). The single
most important promise it makes is: *those credentials only ever travel to
YouTube, and the app never phones home or hands them to a third party.* "Trust
me" is not good enough for a promise like that, so we make it both **structural**
and **observable**:

  • ENFORCE — every server-side HTTP request is checked against a tiny, hard-coded
    allowlist BEFORE the socket opens. A request to any other host raises
    ``BlockedHost`` and never leaves the machine.
  • OBSERVE — every request (allowed or blocked) is written to a rotating network
    log, surfaced read-only at /network so you can watch exactly where the app
    talks.

HOW IT CATCHES *EVERYTHING* (the part that actually matters)
------------------------------------------------------------
The two HTTP stacks this app and its dependencies use each have a single, stable
choke point. We patch each one exactly once, at startup, so there is no way to
make a server-side request that skips this guard:

  • requests  ->  requests.adapters.HTTPAdapter.send
        EVERY requests call funnels through here regardless of which Session made
        it. This matters because ytmusicapi has call sites that bypass the Session
        we hand it (e.g. mixins/uploads.py uses module-level ``requests.post``;
        setup.py and the oauth flow build their own ``requests.Session``). Gating
        at the adapter means we do NOT depend on ytmusicapi's internals — so a
        future ytmusicapi version cannot quietly open a hole. (ytmusicapi is also
        version-capped in pyproject for reproducibility, but this guarantee does
        not rely on that pin.)
  • urllib    ->  urllib.request.OpenerDirector.open
        Our MusicBrainz / Discogs / Last.fm enrichment uses ``urllib.urlopen``,
        which dispatches through ``OpenerDirector.open``.

WHAT THIS DOES *NOT* COVER (stated honestly — also shown on /network)
---------------------------------------------------------------------
Cover-art thumbnails in the UI are loaded by your *browser*, straight from
Google's image CDNs; they never pass through the server, so they do not appear in
this log. No credentials are attached to those image loads.

PRIVACY OF THE LOG ITSELF
-------------------------
We log only method + host + PATH (query strings are stripped, because Last.fm and
Discogs put API keys in the query) + status + byte count. Never headers, cookies,
request bodies, or query parameters.
"""
from __future__ import annotations

import logging
import logging.handlers
from urllib.parse import urlsplit

from yt_playlist.core import paths

# Two distinct loggers, on purpose:
#   • `_access` — the structured request log. Goes ONLY to the rotating network.log
#     file (propagate=False), so /network shows clean verdict lines and nothing else.
#   • `logger`  — operational messages (guard installed, a BLOCK happened). Goes to the
#     normal app console so a refused request is loud where an operator would see it.
_access = logging.getLogger("yt_playlist.network")
logger = logging.getLogger(__name__)

# --- The allowlist -----------------------------------------------------------
# Registrable domains the SERVER is permitted to reach. A request whose host is
# not one of these (or a subdomain of one) is refused before any bytes are sent.
# Keep this list tiny and obvious — every entry is a promise, and anything new
# showing up as BLOCKED on /network is exactly the signal we want to surface.
ALLOWED_DOMAINS = frozenset({
    "youtube.com",        # ytmusicapi — all API + auth traffic goes to music.youtube.com
    "musicbrainz.org",    # enrichment — release metadata
    "discogs.com",        # enrichment — release metadata (api.discogs.com)
    "audioscrobbler.com",  # enrichment — Last.fm API (ws.audioscrobbler.com)
    "last.fm",            # enrichment — Last.fm album HTML page, for the Release Date (www.last.fm)
})


class BlockedHost(Exception):
    """Raised when a server-side request targets a host not on the allowlist."""


def host_allowed(host: str) -> bool:
    """True iff `host` is an allowed domain or a subdomain of one (case-insensitive)."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


class EgressGuard:
    """The one place all server-side outbound HTTP is enforced and logged.

    A single instance is installed at startup (see :func:`install`). It owns the
    rotating log file and the two monkeypatches; the wrappers it installs call
    back into :meth:`gate` (allow/deny decision) and :meth:`record` (logging).
    """

    def __init__(self, log_path=None):
        self.log_path = log_path or paths.network_log_path()
        self._installed = False
        self._orig_adapter_send = None
        self._orig_opener_open = None
        self._configure_logger()

    # -- logging ---------------------------------------------------------------
    def _configure_logger(self) -> None:
        """Point the dedicated network logger at THIS guard's rotating file.

        One file per day, 7 kept. The logger does not propagate, so these lines never
        bleed into the app's console logging — /network is the place to read them.
        Production constructs exactly one guard; we *reset* (rather than accumulate)
        handlers so a re-import (uvicorn --reload) or a second instance in tests can't
        leave the logger writing to a stale file.
        """
        for h in list(_access.handlers):
            _access.removeHandler(h)
            h.close()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            self.log_path, when="midnight", backupCount=7, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _access.addHandler(handler)
        _access.setLevel(logging.INFO)
        _access.propagate = False

    def record(self, *, verdict, method, host, path, status=None, nbytes=None, via) -> None:
        """Write one structured, query-stripped line for a single request."""
        _access.info("verdict=%s via=%s method=%s host=%s path=%s status=%s bytes=%s",
                     verdict, via, method or "?", host or "?", path or "/", status, nbytes)

    # -- the allow/deny decision -----------------------------------------------
    def gate(self, url: str, *, method: str, via: str) -> tuple[str, str]:
        """Check one request. Returns (host, path); raises BlockedHost if off-allowlist.

        `path` is the URL path only — query string is intentionally dropped before
        it ever reaches the log (those carry API keys).
        """
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path or "/"
        if not host_allowed(host):
            # Record the block, shout about it on the main log too, then refuse.
            self.record(verdict="BLOCK", method=method, host=host, path=path, via=via)
            logger.warning("egress BLOCKED: %s %s (host %r not on allowlist)", method, url, host)
            raise BlockedHost(f"egress to {host!r} is not allowed")
        return host, path

    # -- installation (called once, at startup) --------------------------------
    def install(self) -> None:
        """Monkeypatch the two HTTP choke points. Idempotent."""
        if self._installed:
            return
        self._patch_requests()
        self._patch_urllib()
        self._installed = True
        logger.info("egress guard active — allowlist: %s", ", ".join(sorted(ALLOWED_DOMAINS)))

    def _patch_requests(self) -> None:
        import requests.adapters
        guard = self
        self._orig_adapter_send = requests.adapters.HTTPAdapter.send

        def send(adapter_self, request, *args, **kwargs):
            host, path = guard.gate(request.url, method=request.method, via="requests")
            resp = guard._orig_adapter_send(adapter_self, request, *args, **kwargs)
            guard.record(verdict="ALLOW", method=request.method, host=host, path=path,
                         status=getattr(resp, "status_code", None),
                         nbytes=resp.headers.get("Content-Length"), via="requests")
            return resp

        requests.adapters.HTTPAdapter.send = send

    def _patch_urllib(self) -> None:
        import urllib.request
        guard = self
        self._orig_opener_open = urllib.request.OpenerDirector.open

        def open_(opener_self, fullurl, *args, **kwargs):
            # `fullurl` may be a str or a Request; both expose the URL we need.
            url = fullurl.full_url if hasattr(fullurl, "full_url") else fullurl
            method = getattr(fullurl, "get_method", lambda: "GET")()
            host, path = guard.gate(url, method=method, via="urllib")
            resp = guard._orig_opener_open(opener_self, fullurl, *args, **kwargs)
            guard.record(verdict="ALLOW", method=method, host=host, path=path,
                         status=getattr(resp, "status", None),
                         nbytes=resp.headers.get("Content-Length") if hasattr(resp, "headers") else None,
                         via="urllib")
            return resp

        urllib.request.OpenerDirector.open = open_

    # -- read-back for the /network page ---------------------------------------
    def recent(self, limit=200) -> list[str]:
        """Return up to `limit` most-recent log lines (newest last), or [] if none yet."""
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                return [ln.rstrip("\n") for ln in fh.readlines()[-limit:]]
        except FileNotFoundError:
            return []


# The single, process-wide guard. Constructed lazily so merely *importing* this module
# (e.g. in tests) has no filesystem side effect — it only comes to life on install() at
# startup, or the first /network read.
_GUARD: EgressGuard | None = None


def guard() -> EgressGuard:
    """Return the process-wide guard, constructing it on first use."""
    global _GUARD
    if _GUARD is None:
        _GUARD = EgressGuard()
    return _GUARD


def install() -> EgressGuard:
    """Install the process-wide egress guard and return it. Safe to call repeatedly."""
    g = guard()
    g.install()
    return g
