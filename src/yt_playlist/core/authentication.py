"""Recognize expired provider sessions from supplied failures."""
from yt_playlist.core.album_snapshot import LibrarySnapshotError


def is_auth_error(error):
    if isinstance(error, LibrarySnapshotError):
        return error.reason == 'signed out'
    # YouTube may return either HTTP rejection or a signed-out layout which
    # the client cannot parse as an authenticated response.
    text = str(error).lower()
    return ('401' in text or '403' in text or 'unauthorized' in text
            or 'signinendpoint' in text or 'singlecolumnbrowseresultsrenderer' in text)
