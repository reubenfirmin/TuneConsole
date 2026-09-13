"""Public Spotify oEmbed metadata used for source artwork in import reports."""
import functools
import json
import urllib.parse
import urllib.request


_OEMBED = "https://open.spotify.com/oembed"
_USER_AGENT = "TuneConsole/0.1 (https://tuneconsole.com)"
_TIMEOUT_S = 8


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


@functools.lru_cache(maxsize=512)
def thumbnail(album_url):
    """Return Spotify's public thumbnail URL for a canonical album URL, or None."""
    canonical = _canonical_album_url(album_url)
    if canonical is None:
        return None
    query = urllib.parse.urlencode({"url": canonical})
    req = urllib.request.Request(f"{_OEMBED}?{query}", headers={
        "User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as response:
            url = (json.load(response) or {}).get("thumbnail_url")
    except Exception:  # noqa: BLE001 - report artwork is optional
        return None
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
