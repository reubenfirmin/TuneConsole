"""Cover Art Archive: a front cover for a MusicBrainz RELEASE.

Not a Provider. It is the fallback source for cover art, used only when the primary (Deezer) had
none, so it never runs for a track that already has a cover. CAA is keyed by release, not recording,
and the release ids ride along in the recording search MusicBrainz already makes, so resolving one
costs no extra MusicBrainz traffic.

Coverage is patchy (a release often has no art at all, which is a clean 404, not an outage), so we
walk a few candidate releases before giving up.
"""
import json
import logging
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


def front_cover(release_ids):
    """The first front-cover URL across `release_ids`, or None. A 404 means this release simply has
    no art: a miss, not a failure, so we move on to the next candidate without tripping the breaker."""
    for rid in list(release_ids or [])[:_MAX_RELEASES]:
        try:
            data = _get_json(f"{_API}/release/{rid}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                continue                       # no art for this release: try the next one
            _breaker.record(e)
            logger.info("Cover Art Archive error for %s: %s", rid, e)
            continue
        except Exception as e:  # noqa: BLE001 - network/parse: degrade to no art
            _breaker.record(e)
            logger.info("Cover Art Archive failed for %s: %s", rid, e)
            continue
        _breaker.record()
        for img in (data.get("images") or []):
            if not img.get("front"):
                continue
            # Prefer a sized thumbnail: the raw `image` is often several MB, far past what a
            # 40px table row or a mosaic tile needs.
            thumbs = img.get("thumbnails") or {}
            url = thumbs.get("500") or thumbs.get("large") or thumbs.get("250") or img.get("image")
            if url:
                # CAA hands back http:// urls, and the browser loads these directly. Keep them off
                # plaintext.
                return url.replace("http://", "https://", 1) if url.startswith("http://") else url
    return None
