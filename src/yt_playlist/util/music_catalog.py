"""Normalize native music catalog identities without acquiring library ownership."""
import re

from yt_playlist.util.duration import parse_duration

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{3,150}\Z')
ARTIST_ID = re.compile(r'UC[A-Za-z0-9_-]{6,98}\Z')

# Stable across sources and query types: creator, recording, release, collection.
# Missing sections simply disappear without changing the remaining order.
SEARCH_SECTIONS = (('artist', 'Artists'), ('song', 'Songs'), ('album', 'Albums'), ('playlist', 'Playlists'))


def section_order(kind):
    return tuple(row[0] for row in SEARCH_SECTIONS).index('song' if kind == 'track' else kind)


def thumbnail(rows):
    for row in reversed(rows or []):
        url = row.get('url', '')
        if url.startswith('https://'):
            return url
    return ''


def artist(row):
    browse_id = row.get('browseId') or row.get('channelId') or ''
    name = row.get('artist') or row.get('name') or row.get('title') or ''
    if not browse_id:
        # Native top results carry the artist's explicit ID/name pair here.
        # Multiple credits do not identify a single navigable artist.
        credits = row.get('artists')
        if isinstance(credits, list) and len(credits) == 1 and isinstance(credits[0], dict):
            browse_id = credits[0].get('id') or ''
            name = credits[0].get('name') or ''
    if not isinstance(browse_id, str) or not ARTIST_ID.fullmatch(browse_id) or not isinstance(name, str) or not name.strip():
        return None
    return {'browse_id': browse_id, 'name': name.strip()[:160],
            'thumbnail': thumbnail(row.get('thumbnails'))}


def song(row):
    video = row.get('videoId')
    if not isinstance(video, str) or not VIDEO_ID.fullmatch(video) or not row.get('title'):
        return None
    if row.get('isAvailable') is False:
        return None
    credits = row.get('artists') or []
    album = row.get('album') or {}
    return dict(video_id=video, title=row['title'],
                artist=', '.join(item['name'] for item in credits if item.get('name')),
                album=album.get('name', ''), album_browse_id=album.get('id'),
                artist_browse_id=next((item.get('id') for item in credits if item.get('id')), None),
                thumbnail=thumbnail(row.get('thumbnails')), credits=credits,
                duration_s=row.get('duration_seconds') or parse_duration(row.get('duration')),
                duration=row.get('duration') or '', video_type=row.get('videoType'))
