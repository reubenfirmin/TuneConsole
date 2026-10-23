"""Public Spotify oEmbed metadata used for source artwork in import reports."""
from collections import OrderedDict
import json
import threading
import time
import urllib.parse
import urllib.request
import urllib.error


_OEMBED = "https://open.spotify.com/oembed"
_USER_AGENT = "TuneConsole/0.1 (https://tuneconsole.com)"
_TIMEOUT_S = 8
_HIT_TTL_S = 86400
_MISS_TTL_S = 3600
_RETRY_S = 60
_CACHE_LIMIT = 512
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _canonical_album_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return None
    path = parts.path.rstrip("/").split("/")
    if (parts.scheme != "https" or parts.hostname != "open.spotify.com" or len(path) != 3
            or path[1] != "album" or not path[2].isalnum()):
        return None
    return f"https://open.spotify.com/album/{path[2]}"


def thumbnail(album_url):
    """Return Spotify's public thumbnail URL for a canonical album URL, or None."""
    canonical = _canonical_album_url(album_url)
    if canonical is None:
        return None
    with _cache_lock:
        cached = _cache.get(canonical)
        if cached and time.monotonic() < cached[0]:
            _cache.move_to_end(canonical)
            return cached[1]
    try:
        url = _fetch_thumbnail(canonical)
    except Exception:  # report artwork is optional, but failures must expire promptly
        url, ttl = None, _RETRY_S
    else:
        ttl = _HIT_TTL_S if url else _MISS_TTL_S
    with _cache_lock:
        _cache[canonical] = (time.monotonic() + ttl, url)
        _cache.move_to_end(canonical)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return url


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _fetch_thumbnail(canonical):
    query = urllib.parse.urlencode({"url": canonical})
    req = urllib.request.Request(f"{_OEMBED}?{query}", headers={
        "User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as response:
            url = json.load(response).get("thumbnail_url")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        raise
    try:
        parts = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return None
    host = (parts.hostname or "").lower()
    spotify_image_host = any(
        host == domain or host.endswith("." + domain)
        for domain in ("scdn.co", "spotifycdn.com")
    )
    return url if parts.scheme == "https" and spotify_image_host else None
