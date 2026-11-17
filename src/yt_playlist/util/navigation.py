"""Internal detail URLs shared by controllers, templates and browser navigation."""
from collections.abc import Mapping
from urllib.parse import quote, urlencode


# The browser receives this same contract, including the ordered preview hints.
LINK_CONFIG = {
    'routes': {'artist': '/artist', 'track': '/track/', 'playlist': '/playlist/',
               'saved_playlist': '/playlist/saved/', 'album': '/album'},
    'track_hints': [
        ['title', ['title']], ['artist', ['artist']], ['album', ['album']],
        ['thumbnail', ['thumbnail']], ['duration', ['duration']],
        ['album_browse', ['album_browse', 'album_browse_id']],
        ['artist_browse', ['artist_browse', 'artist_browse_id']],
    ],
}


def artist_url(artist_id=None, *, browse=None):
    """Prefer our identity; an external artist requires an explicit channel ID."""
    query = ({'artist_id': artist_id} if artist_id is not None else
             {'browse': browse} if browse else None)
    return LINK_CONFIG['routes']['artist'] + '?' + urlencode(query) if query else None


def artist_credit_links(credits):
    return [{**row, 'href': artist_url(row.get('artist_id'), browse=row.get('browse'))} for row in credits]


def album_url(browse):
    return LINK_CONFIG['routes']['album'] + '?' + urlencode({'browse': browse}) if browse else None


def playlist_url(playlist_id=None, *, account_id=None, provider_id=None):
    if playlist_id is not None:
        return LINK_CONFIG['routes']['playlist'] + quote(str(playlist_id), safe='')
    if account_id is not None and provider_id:
        return (LINK_CONFIG['routes']['saved_playlist'] + quote(str(account_id), safe='') +
                '/' + quote(str(provider_id), safe=''))
    return None


def track_url(song, *, page=None):
    """Preserve preview metadata for songs outside the local catalog."""
    if isinstance(song, str):
        song = {'video_id': song}
    get = song.get if isinstance(song, Mapping) else lambda field: getattr(song, field, None)
    video = get('video_id') or get('videoId') or get('vid')
    if not video:
        return ''
    hints = {field: next((get(key) for key in keys if get(key) is not None and get(key) != ''), None)
             for field, keys in LINK_CONFIG['track_hints']}
    if page is not None:
        hints['page'] = page
    query = urlencode({key: value for key, value in hints.items() if value is not None})
    return LINK_CONFIG['routes']['track'] + quote(str(video), safe='') + ('?' + query if query else '')
