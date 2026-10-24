"""Internal song links retain preview metadata for tracks outside the local catalog."""
from collections.abc import Mapping
from urllib.parse import quote, urlencode


def track_url(song):
    get = song.get if isinstance(song, Mapping) else lambda field: getattr(song, field, None)
    video = get('video_id') or get('videoId')
    if not video:
        return ''
    hints = {field: get(field) for field in
             ('title', 'artist', 'album', 'thumbnail', 'duration')}
    hints['album_browse'] = get('album_browse') or get('album_browse_id')
    query = urlencode({key: value for key, value in hints.items()
                       if value is not None and value != ''})
    return '/track/' + quote(str(video), safe='') + ('?' + query if query else '')
