"""
==============================================================================
  EGRESS GUARD: every outbound network request the SERVER makes is gated here
==============================================================================

WHY THIS CLASS EXISTS (read this before changing anything)
----------------------------------------------------------
The browser bridge keeps Google session cookies in the browser. The server does
not store Google cookies or OAuth tokens. "Trust me" is not good enough for a
promise like that, so we make its network access both **structural** and
**observable**:

  • ENFORCE: every server-side HTTP request is checked against a tiny, hard-coded
    allowlist BEFORE the socket opens. A request outside that policy raises
    ``BlockedHost`` and never leaves the machine.
  • OBSERVE: every request (allowed or blocked) is written to a rotating network
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
        at the adapter means we do NOT depend on ytmusicapi's internals, so a
        future ytmusicapi version cannot quietly open a hole. (ytmusicapi is also
        version-capped in pyproject for reproducibility, but this guarantee does
        not rely on that pin.)
  • urllib    ->  urllib.request.OpenerDirector.open
        Our MusicBrainz / Discogs / Last.fm enrichment uses ``urllib.urlopen``,
        which dispatches through ``OpenerDirector.open``. Its ``_open`` transport
        hook records each response before redirects and HTTP errors are processed.

WHAT THIS DOES *NOT* COVER (stated honestly, also shown on /network)
---------------------------------------------------------------------
Cover-art thumbnails in the UI are loaded by your *browser*, straight from
Google's image CDNs; they never pass through the server, so they do not appear in
this log. No credentials are attached to those image loads.
TV's static-video check separately fetches timeline preview frames on the
server. Only bodyless HTTPS GETs to YouTube's storyboard image paths are allowed;
these requests are logged, and their signed query strings are omitted.

PRIVACY OF THE LOG ITSELF
-------------------------
We log only method + host + PATH (query strings are stripped, because Last.fm and
Discogs put API keys in the query) + status + byte count. Never headers, cookies,
request bodies, or query parameters.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from yt_playlist.core import paths

# Two distinct loggers, on purpose:
#   • `_access`: the structured request log. Goes ONLY to the rotating network.log
#     file (propagate=False), so /network shows clean verdict lines and nothing else.
#   • `logger`: operational messages (guard installed, a BLOCK happened). Goes to the
#     normal app console so a refused request is loud where an operator would see it.
_access = logging.getLogger("yt_playlist.network")
logger = logging.getLogger(__name__)

# --- The allowlist -----------------------------------------------------------
# Registrable domains the SERVER is permitted to reach. A request whose host is
# not one of these (or a subdomain of one) needs an exact request rule below.
# Keep this list tiny and obvious. Every entry is a promise, and anything new
# showing up as BLOCKED on /network is exactly the signal we want to surface.
ALLOWED_DOMAINS = frozenset({
    "youtube.com",        # ytmusicapi: all API + auth traffic goes to music.youtube.com
    "musicbrainz.org",    # enrichment: release metadata
    "discogs.com",        # enrichment: release metadata (api.discogs.com)
    "audioscrobbler.com",  # enrichment: Last.fm API (ws.audioscrobbler.com)
    "last.fm",            # enrichment: Last.fm album HTML page, for the Release Date (www.last.fm)
    "deezer.com",         # enrichment: BPM (api.deezer.com), no auth
    "acousticbrainz.org",  # enrichment: BPM/energy/danceability by MusicBrainz recording MBID
    "coverartarchive.org",  # enrichment: album art by MusicBrainz release MBID, no auth
    "archive.org",        # where coverartarchive.org redirects its JSON + images (its CDN)
    "wikipedia.org",      # into-recently card: keyless page summaries (en.wikipedia.org)
    "spotify.com",        # Spotify import report: public oEmbed album artwork
})

# Exact, bodyless requests only: GitHub hosts arbitrary user content, so allowing
# the entire domain would also permit unrelated repositories and API operations.
# Match the full URL to reject query strings, fragments, credentials and ports.
ALLOWED_REQUESTS = frozenset({
    ("GET", "https://api.github.com/repos/reubenfirmin/TuneConsole/releases/latest"),
})

# Optional user-key album descriptions. This permits one HTTPS POST endpoint,
# not domain-wide DeepSeek access. Headers and bodies never enter network logs.
ALLOWED_POST_REQUESTS = frozenset({
    ('POST', 'https://api.deepseek.com/chat/completions'),
})


class BlockedHost(Exception):
    """Raised when a server-side request is outside the allowlist policy."""


def host_allowed(host: str) -> bool:
    """True iff `host` is an allowed domain or a subdomain of one (case-insensitive)."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def storyboard_url_allowed(url: str) -> bool:
    """Only YouTube's signed timeline sheets, without broad image-CDN access."""
    parts = urlsplit(url)
    return (len(url) <= 4096 and parts.scheme == 'https' and parts.netloc == 'i.ytimg.com'
            and not parts.fragment and re.fullmatch(
                r'/sb/[A-Za-z0-9_-]{11}/storyboard\d+_L\d+/[A-Za-z0-9_-]{1,40}\.jpg',
                parts.path) is not None)


class EgressGuard:
    """The one place all server-side outbound HTTP is enforced and logged.

    A single instance is installed at startup (see :func:`install`). It owns the
    rotating log file and the transport hooks; the wrappers it installs call
    back into :meth:`gate` (allow/deny decision) and :meth:`record` (logging).
    """

    def __init__(self, log_path=None):
        self.log_path = log_path or paths.network_log_path()
        self._installed = False
        self._orig_adapter_send = None
        self._orig_opener_open = None
        self._orig_opener_send = None
        self._configure_logger()

    # -- logging ---------------------------------------------------------------
    def _configure_logger(self) -> None:
        """Point the dedicated network logger at THIS guard's rotating file.

        One file per day, 7 kept. The logger does not propagate, so these lines never
        bleed into the app's console logging. /network is the place to read them.
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

    def record(self, *, verdict, method, host, path, status=None, nbytes=None, via, failure=None) -> None:
        """Write one structured, query-stripped line for a single request."""
        _access.info("verdict=%s via=%s method=%s host=%s path=%s status=%s bytes=%s failure=%s",
                     verdict, via, method or "?", host or "?", path or "/", status, nbytes, failure)

    def _record_failure(self, error, **request):
        """Classify without recording exception text, which can contain credentials."""
        import requests.exceptions

        reason = error.reason if isinstance(error, URLError) else error
        status = error.code if isinstance(error, HTTPError) else getattr(
            getattr(error, "response", None), "status_code", None)
        if isinstance(error, (HTTPError, requests.exceptions.HTTPError)):
            category = "http"
        elif isinstance(reason, (TimeoutError, requests.exceptions.Timeout)):
            category = "timeout"
        elif isinstance(reason, (ssl.SSLError, requests.exceptions.SSLError)):
            category = "tls"
        elif isinstance(error, (URLError, OSError, requests.exceptions.ConnectionError)):
            category = "connection"
        else:
            category = "transport"
        self.record(verdict="ALLOW", status=status, failure=category, **request)

    def _record_response(self, response, *, status, **request):
        self.record(verdict="ALLOW", status=status,
                    nbytes=response.headers.get("Content-Length") if hasattr(response, "headers") else None,
                    failure="http" if isinstance(status, int) and status >= 400 else None, **request)

    # -- the allow/deny decision -----------------------------------------------
    def gate(self, url: str, *, method: str, via: str, body=None) -> tuple[str, str]:
        """Check one request. Returns (host, path); raises BlockedHost if off-allowlist.

        `path` is the URL path only. Query string is intentionally dropped before
        it ever reaches the log (those carry API keys). Exact request rules also
        require an absent body; domain-wide rules retain their existing behavior.
        """
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path or "/"
        exact_request_allowed = ((body is None and (method.upper(), url) in ALLOWED_REQUESTS)
                                 or (method.upper(), url) in ALLOWED_POST_REQUESTS)
        preview_allowed = body is None and method.upper() == 'GET' and storyboard_url_allowed(url)
        if not host_allowed(host) and not exact_request_allowed and not preview_allowed:
            # Record the block, shout about it on the main log too, then refuse.
            self.record(verdict="BLOCK", method=method, host=host, path=path, via=via)
            logger.warning("egress BLOCKED: %s %s%s", method, host, path)
            raise BlockedHost(f"egress request to {host!r} is not allowed")
        return host, path

    # -- installation (called once, at startup) --------------------------------
    def install(self) -> None:
        """Monkeypatch the two HTTP choke points. Idempotent."""
        if self._installed:
            return
        self._patch_requests()
        self._patch_urllib()
        self._installed = True
        logger.info("egress guard active, allowlist: %s", ", ".join(sorted(ALLOWED_DOMAINS)))
        logger.info("egress exact bodyless requests: %s",
                    ", ".join(f"{method} {url}" for method, url in sorted(ALLOWED_REQUESTS)))

    def _patch_requests(self) -> None:
        import requests.adapters
        guard = self
        self._orig_adapter_send = requests.adapters.HTTPAdapter.send

        def send(adapter_self, request, *args, **kwargs):
            host, path = guard.gate(request.url, method=request.method, via="requests",
                                   body=request.body)
            details = dict(method=request.method, host=host, path=path, via="requests")
            try:
                resp = guard._orig_adapter_send(adapter_self, request, *args, **kwargs)
            except BlockedHost:
                raise
            except Exception as error:
                guard._record_failure(error, **details)
                raise
            guard._record_response(resp, status=getattr(resp, "status_code", None), **details)
            return resp

        requests.adapters.HTTPAdapter.send = send

    def _patch_urllib(self) -> None:
        import urllib.request
        guard = self
        self._orig_opener_open = urllib.request.OpenerDirector.open
        self._orig_opener_send = urllib.request.OpenerDirector._open

        def open_(opener_self, fullurl, *args, **kwargs):
            # `fullurl` may be a str or a Request; both expose the URL we need.
            url = fullurl.full_url if hasattr(fullurl, "full_url") else fullurl
            data = kwargs.get("data", args[0] if args else None)
            if data is None:
                data = getattr(fullurl, "data", None)
            method = getattr(fullurl, "get_method", lambda: "GET")()
            # OpenerDirector applies its data argument after this wrapper runs;
            # account for the resulting implicit POST before deciding the policy.
            if data is not None and getattr(fullurl, "method", None) is None:
                method = "POST"
            guard.gate(url, method=method, via="urllib", body=data)
            return guard._orig_opener_open(opener_self, fullurl, *args, **kwargs)

        def send(opener_self, request, *args, **kwargs):
            # Observe before response processors follow redirects or raise HTTPError.
            # Each actual hop is logged once, with its own status; an inner failure
            # cannot be counted again as each redirected open() unwinds.
            method = request.get_method()
            host, path = guard.gate(request.full_url, method=method, via="urllib", body=request.data)
            details = dict(method=method, host=host, path=path, via="urllib")
            try:
                resp = guard._orig_opener_send(opener_self, request, *args, **kwargs)
            except BlockedHost:
                raise
            except Exception as error:
                guard._record_failure(error, **details)
                raise
            guard._record_response(resp, status=getattr(resp, "status", None), **details)
            return resp

        urllib.request.OpenerDirector.open = open_
        urllib.request.OpenerDirector._open = send

    # -- read-back for the /network page ---------------------------------------
    def recent(self, limit=200) -> list[str]:
        """Return up to `limit` most-recent log lines (newest last), or [] if none yet."""
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                return [ln.rstrip("\n") for ln in fh.readlines()[-limit:]]
        except FileNotFoundError:
            return []


# The single, process-wide guard. Constructed lazily so merely *importing* this module
# (e.g. in tests) has no filesystem side effect. It only comes to life on install() at
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
