"""Pick a reliable thumbnail URL from a ytmusicapi thumbnails list.

ytmusicapi returns thumbnails smallest-first. Naively taking the largest (`[-1]`) breaks for
video-backed entries, whose largest is `…/maxresdefault.jpg`, a size YouTube only generates for
some videos, so it 404s for the rest. `hqdefault.jpg` always exists, so we downgrade to it.
"""


def best_thumb(thumbnails):
    if not thumbnails:
        return None
    url = (thumbnails[-1] or {}).get("url")
    if not url:
        return None
    return url.replace("maxresdefault", "hqdefault")
