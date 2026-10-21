"""Keep full fetched album track lists for measured listening coverage."""
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.library.album_fetch import shareable_album_tracks


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
    keys = sorted({store.observe_track(t.get("videoId"), t["title"], artists(t) or artist,
                                      album=title,album_browse_id=browse_id) for t in tracks})
    try:
        video_ids = [track["videoId"] for track in shareable_album_tracks(album)]
    except ValueError:
        video_ids = None
    store.collection.cache_album_tracklist(
        browse_id, title, artist, best_thumb(album.get("thumbnails")) or fallback.get("thumbnail"), keys,
        video_ids=video_ids)
