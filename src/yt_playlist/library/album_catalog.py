"""Keep full fetched album track lists for measured listening coverage."""
from yt_playlist.util.matching import identity_key
from yt_playlist.util.thumbnails import best_thumb


def remember_album(store, browse_id, album, fallback=None):
    """Reject partial/anonymous responses. Counts describe distinct songs, not completion."""
    fallback = fallback or {}
    tracks = (album or {}).get("tracks") or []
    if not tracks or album.get("continuations") or album.get("continuation"):
        return
    advertised = album.get("trackCount")
    if advertised is not None and str(advertised).isdigit() and int(advertised) != len(tracks):
        return
    def artists(value):
        return ", ".join(a.get("name", "") for a in value.get("artists", []) or [] if a.get("name"))

    artist = artists(album) or fallback.get("artist") or ""
    title = album.get("title") or fallback.get("title")
    if not title or not artist or any(not t.get("title") for t in tracks):
        return
    keys = sorted({identity_key(t["title"], artists(t) or artist) for t in tracks})
    store.collection.cache_album_tracklist(
        browse_id, title, artist, best_thumb(album.get("thumbnails")) or fallback.get("thumbnail"), keys)
