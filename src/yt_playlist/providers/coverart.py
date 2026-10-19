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
            return cached.url

        failures = 0
        ttl = _MISS_TTL_S
        url = None
        try:
            data = _get_json(f"{_API}/release/{rid}")
            url = _front_url(data)
        except urllib.error.HTTPError as e:
            _breaker.record(e)
            if e.code not in (404, 400):
                failures = min((cached.failures if cached else 0) + 1, 5)
                ttl = min(_ERROR_RETRY_S * 2 ** (failures - 1), _MAX_ERROR_RETRY_S)
                logger.info("Cover Art Archive error for %s: %s; retrying in %ss", rid, e, ttl)
        except Exception as e:  # noqa: BLE001 - network/parse: degrade to no art
            _breaker.record(e)
            failures = min((cached.failures if cached else 0) + 1, 5)
            ttl = min(_ERROR_RETRY_S * 2 ** (failures - 1), _MAX_ERROR_RETRY_S)
            logger.info("Cover Art Archive failed for %s: %s; retrying in %ss", rid, e, ttl)
        else:
            _breaker.record()
            if url:
                ttl = _COVER_TTL_S

        # Start the TTL after the response, not before a potentially slow network request.
        _cache[rid] = _CachedCover(url, time.monotonic() + ttl, failures)
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
