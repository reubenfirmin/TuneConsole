"""Pick a reliable thumbnail URL from a ytmusicapi thumbnails list.

ytmusicapi returns thumbnails smallest-first. Naively taking the largest (`[-1]`) breaks for
video-backed entries, whose largest is `…/maxresdefault.jpg`, a size YouTube only generates for
some videos, so it 404s for the rest. `hqdefault.jpg` always exists, so we downgrade to it.
"""

# YouTube does not 404 when it has no cover: it serves a generic grey disc from this path, so a
# thumbnail URL is not proof of a cover. Uploads (privately owned releases) get these almost always,
# since YouTube stores no art for your own files. Both variants occur in the wild:
# cover_track_default and cover_album_default.
_PLACEHOLDER_MARKERS = ("cover_track_default", "cover_album_default")


def is_placeholder_art(url) -> bool:
    """Is this YouTube's stand-in image rather than a real cover? Such art must not block a provider
    from filling in the real thing, but real art must never be overwritten."""
    return any(m in url for m in _PLACEHOLDER_MARKERS) if url else False


def best_thumb(thumbnails):
    if not thumbnails:
        return None
    url = (thumbnails[-1] or {}).get("url")
    if not url:
        return None
    return url.replace("maxresdefault", "hqdefault")
