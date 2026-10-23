"""Cover Art Archive: a front cover for a MusicBrainz RELEASE.

Not a Provider. It is the fallback source for cover art, used only when the primary (Deezer) had
none, so it never runs for a track that already has a cover. CAA is keyed by release, not recording,
and the release ids ride along in the recording search MusicBrainz already makes, so resolving one
costs no extra MusicBrainz traffic.

Coverage is patchy (a release often has no art at all, which is a clean 404, not an outage), so we
walk a few candidate releases before giving up. Results are shared across tracks from the same
release; transient errors get a short, increasing retry delay rather than one request per track.
"""
from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers.base import RateLimiter

logger = logging.getLogger(__name__)

_API = "https://coverartarchive.org"
_USER_AGENT = "yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )"
_HTTP_TIMEOUT_S = 20
_MAX_RELEASES = 4          # candidates to try before accepting that there is no art
_MIN_INTERVAL = 0.25
_pacer = RateLimiter(_MIN_INTERVAL)
_breaker = net.CircuitBreaker()
_CACHE_SIZE = 1024
_COVER_TTL_S = 24 * 60 * 60
_MISS_TTL_S = 6 * 60 * 60
_ERROR_RETRY_S = 60
_MAX_ERROR_RETRY_S = 15 * 60


@dataclass
class _CachedCover:
    url: str | None
    expires_at: float
    failures: int = 0
    error: str = ""


class _Retry(Exception):
    def __init__(self, error, retry_in):
        super().__init__(error)
        self.retry_in = retry_in


@dataclass(frozen=True)
class CoverResult:
    status: str                         # found, absent, retry
    url: str | None = None
    retry_in: float = 0
    error: str = ""


_cache: OrderedDict[str, _CachedCover] = OrderedDict()
# Serialize lookup + cache fill so overlapping enrichment jobs share the same HTTP request.
# No store lock is held here, and each request retains its normal network timeout.
_lookup_lock = threading.Lock()


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def _get_json(url):
    _pacer.wait()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.load(resp)


def _front_url(data):
    for img in (data.get("images") or []):
        if not img.get("front"):
            continue
        # The original image can be several MB; a sized thumbnail is enough for a table or mosaic.
        thumbs = img.get("thumbnails") or {}
        url = thumbs.get("500") or thumbs.get("large") or thumbs.get("250") or img.get("image")
        if url:
            return url.replace("http://", "https://", 1) if url.startswith("http://") else url
    return None


def _release_cover(rid):
    with _lookup_lock:
        cached = _cache.get(rid)
        if cached and time.monotonic() < cached.expires_at:
            _cache.move_to_end(rid)
            if cached.failures:
                net.record_probe_failure(_Retry(cached.error, cached.expires_at - time.monotonic()))
            return cached.url

        failures = 0
        ttl = _MISS_TTL_S
        url = None
        error = ""
        try:
            data = _get_json(f"{_API}/release/{rid}")
            url = _front_url(data)
        except urllib.error.HTTPError as e:
            _breaker.record() if e.code in (404, 400) else _breaker.record(e)
            if e.code not in (404, 400):
                error = str(e)
                failures = min((cached.failures if cached else 0) + 1, 5)
                ttl = max(min(_ERROR_RETRY_S * 2 ** (failures - 1), _MAX_ERROR_RETRY_S),
                          net.retry_after(e, time.time()) - time.time())
                logger.info("Cover Art Archive error for %s: %s; retrying in %ss", rid, e, ttl)
        except Exception as e:  # noqa: BLE001 - network/parse: degrade to no art
            _breaker.record(e)
            error = str(e) or type(e).__name__
            failures = min((cached.failures if cached else 0) + 1, 5)
            ttl = min(_ERROR_RETRY_S * 2 ** (failures - 1), _MAX_ERROR_RETRY_S)
            logger.info("Cover Art Archive failed for %s: %s; retrying in %ss", rid, e, ttl)
        else:
            _breaker.record()
            if url:
                ttl = _COVER_TTL_S

        # Start the TTL after the response, not before a potentially slow network request.
        _cache[rid] = _CachedCover(url, time.monotonic() + ttl, failures, error)
        if failures:
            net.record_probe_failure(_Retry(error, ttl))
        _cache.move_to_end(rid)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
        return url


def front_cover(release_ids):
    """First cover from up to four distinct releases, including cached hits and misses.

    Errors delay only that release, so another edition can still supply the album's art.
    Expired failures are retried lazily on a later lookup, without blocking enrichment to sleep.
    """
    for rid in list(dict.fromkeys(release_ids or []))[:_MAX_RELEASES]:
        url = _release_cover(rid)
        if url:
            return url
    return None


def lookup(release_ids):
    """Retain the distinction between a confirmed miss and a deferred archive request."""
    with net.probe_errors() as errors:
        try:
            url = front_cover(release_ids)
        except Exception as exc:
            net.record_probe_failure(exc)
            url = None
    if url:
        return CoverResult("found", url)
    if errors:
        now = time.time()
        delay = max([_ERROR_RETRY_S, *(getattr(e, "retry_in", 0) for e in errors),
                     *(net.retry_after(e, now) - now for e in errors)])
        return CoverResult("retry", retry_in=delay, error=str(errors[-1]))
    return CoverResult("absent")
