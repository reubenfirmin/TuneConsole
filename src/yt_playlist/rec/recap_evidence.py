"""Evidence interpretation shared by monthly recap preparation."""
from datetime import datetime
from zoneinfo import ZoneInfo

QUARANTINE_GROUP = 'Generated'
CREATION_SOURCES = ('local', 'youtube')
CALENDAR_MARGIN_SECONDS = 86400
TASTE_FILTER = """COALESCE(is_generated,0)=0 AND NOT EXISTS (
    SELECT 1 FROM quarantined_playlists q JOIN playlist_scopes s ON s.id=q.playlist_scope_id
    WHERE q.name=? AND s.provider_playlist_id=pe.playlist_ytm_id
    AND (s.account_id IS NULL OR s.account_id=pe.account_id))"""


def metadata(rows, releases, credits):
    """Choose fields independently across uploads; ambiguous release context stays unknown."""
    result = {}
    for row in rows:
        card = result.setdefault(row['identity_key'], {})
        for field in ('title', 'artist', 'genre', 'mb_year'):
            if row[field] and not card.get(field):
                card[field] = row[field]
    for key, browse in releases:
        if key in result:
            result[key].setdefault('album_browse_ids', set()).add(browse)
    for card in result.values():
        albums = card.get('album_browse_ids', set())
        card['album_browse_id'] = next(iter(albums)) if len(albums) == 1 else None
    for key, artist in credits:
        if key in result:
            result[key].setdefault('artist_ids', []).append(artist)
    return result


def first_heard(plays, observations):
    """Coarse observations can disprove novelty but never add exact play counts."""
    result = dict(plays)
    for key, day in observations.items():
        result[key] = min(result.get(key, day), day)
    return result


def albums(rows):
    result = {}
    for row in rows:
        album = result.setdefault(row['id'], {key: row[key] for key in ('title', 'artist', 'thumbnail', 'browse_id')})
        album.setdefault('keys', set()).add(row['track_key'])
    return list(result.values())


def playlist_dates(rows, calendars):
    result = []
    for row in rows:
        playlist = dict(row)
        zone = ZoneInfo(calendars.get(playlist.pop('account_id'), 'UTC'))
        playlist['day'] = datetime.fromtimestamp(playlist['created_at'], zone).date().isoformat()
        result.append(playlist)
    return result
