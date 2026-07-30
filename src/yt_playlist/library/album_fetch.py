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
