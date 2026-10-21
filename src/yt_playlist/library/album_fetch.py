"""Live album metadata, routed by album kind.

YouTube Music has two kinds of album: catalog releases (browse ids starting MPRE) and the releases
you uploaded yourself ("privately owned"), whose detail browse ids start with
FEmusic_library_privately_owned_release_detail. ytmusicapi's get_album() raises on any non-MPRE id
BEFORE it makes a request, so an uploaded album must go through get_library_upload_album() instead.
That is the whole of #107: an uploaded album is never in saved_albums (saving means liking an
audioPlaylistId, which an upload has none of), so it also had no local fallback to save it, and the
page dead-ended on "Album unavailable".

Both endpoints return the fields callers need (title/thumbnails/artists/year, and tracks carrying
videoId/title/artists/duration), so callers do not care which one ran.
"""

UPLOAD_RELEASE_PREFIX = "FEmusic_library_privately_owned_release_detail"


def is_upload_album(browse_id: str | None) -> bool:
    return (browse_id or "").startswith(UPLOAD_RELEASE_PREFIX)


def fetch_album(client, browse_id: str) -> dict:
    if is_upload_album(browse_id):
        return client.get_library_upload_album(browse_id)
    return client.get_album(browse_id)


def shareable_album_tracks(album):
    """A complete ordered response, with a shareable video for every advertised track.

    Neither album endpoint has a supported generic continuation API here. Decline unfinished
    responses rather than exporting a prefix or silently dropping unsupported/unavailable items.
    """
    tracks = album.get("tracks") if isinstance(album, dict) else None
    if not isinstance(tracks, list) or not tracks or album.get("continuation") or album.get("continuations"):
        raise ValueError("The full album track list is unavailable. Reconnect to YouTube Music and try sharing again.")
    count = album.get("trackCount")
    if count is not None:
        try:
            complete = int(str(count).replace(",", "")) == len(tracks)
        except (TypeError, ValueError):
            complete = False
        if not complete:
            raise ValueError("The album track list is incomplete. Refresh the album and try sharing again.")
    if any(not isinstance(track, dict) or not isinstance(track.get("videoId"), str)
           or not track["videoId"].strip() for track in tracks):
        raise ValueError("Some album tracks have no shareable video link. Try again when all tracks are available.")
    return tracks
