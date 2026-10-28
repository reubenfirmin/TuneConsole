"""Pick a reliable thumbnail URL from a ytmusicapi thumbnails list.

ytmusicapi returns thumbnails smallest-first. Naively taking the largest (`[-1]`) breaks for
video-backed entries, whose largest is `…/maxresdefault.jpg`, a size YouTube only generates for
some videos, so it 404s for the rest. `hqdefault.jpg` always exists, so we downgrade to it.
"""
import re
from urllib.parse import urlsplit, urlunsplit

# YouTube does not 404 when it has no cover: it serves a generic grey disc from this path, so a
# thumbnail URL is not proof of a cover. Uploads (privately owned releases) get these almost always,
# since YouTube stores no art for your own files. Both variants occur in the wild:
# cover_track_default and cover_album_default.
_PLACEHOLDER_MARKERS = ("cover_track_default", "cover_album_default")


def is_placeholder_art(url) -> bool:
    """Identify YouTube's stand-in image, which cannot displace or block a useful cover."""
    return any(m in url for m in _PLACEHOLDER_MARKERS) if url else False


def best_thumb(thumbnails):
    if not thumbnails:
        return None
    url = (thumbnails[-1] or {}).get("url")
    if not url:
        return None
    return url.replace("maxresdefault", "hqdefault")


def detail_thumb(url):
    """Request album artwork at the detail view's size from its existing source."""
    try:
        parts = urlsplit(url)
        if (parts.hostname or '').endswith('.googleusercontent.com'):
            return urlunsplit(parts._replace(path=re.sub(
                r'=w\d+-h\d+(?=-|$)', '=w800-h800', parts.path)))
    except ValueError:
        pass
    return url
