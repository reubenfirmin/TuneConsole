"""Reconcile exact provider representations, never releases inferred from a title."""
import hashlib
import json
from yt_playlist.library.album_fetch import shareable_album_tracks
from yt_playlist.util.duration import parse_duration


def _text(value):
    return ' '.join(str(value or '').split()).casefold()


def fingerprint(album):
    """Missing comparison fields are unknown, rather than evidence of equality."""
    try:
        tracks = shareable_album_tracks(album)
    except (ValueError, AttributeError):
        return None
    title, year = _text(album.get('title')), str(album.get('year') or '')
    artists = album.get('artists') or []
    if not isinstance(artists, list) or any(not isinstance(a, dict) for a in artists):
        return None
    credits = [_text(a.get('name')) for a in artists]
    if not title or len(year) != 4 or not year.isdigit() or not credits or not all(credits):
        return None
    ordered = []
    for track in tracks:
        duration = parse_duration(track.get('duration'))
        explicit = track.get('isExplicit')
        if not _text(track.get('title')) or duration is None or duration <= 0 or type(explicit) is not bool:
            return None
        ordered.append([track['videoId'], _text(track['title']), duration, explicit,
                        track.get('discNumber'), track.get('trackNumber')])
    encoded = json.dumps(['youtube-album-v1', title, credits, year, _text(album.get('type')), ordered],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest(), [t['videoId'] for t in tracks]


def primary_id(conn, browse_id):
    row = conn.execute("SELECT preferred.provider_id FROM album_provider_ids p "
        "JOIN album_provider_ids preferred ON preferred.album_id=p.album_id "
        "AND preferred.provider=p.provider AND preferred.is_primary=1 "
        "WHERE p.provider='youtube' AND p.provider_id=?",
        (browse_id,)).fetchone()
    return row[0] if row else browse_id



def compatible(inputs, videos):
    """Require every retained representation to agree with the complete response."""
    for revision in inputs['revisions']:
        occurrences = revision['occurrences']
        if any(row['video_id'] not in videos for row in occurrences):
            return False
        if revision['status'] == 'complete' and (revision['order_status'] != 'known' or
                [row['video_id'] for row in occurrences] != videos):
            return False
        if any(row['position'] is not None and (row['position'] >= len(videos) or
                videos[row['position']] != row['video_id']) for row in occurrences):
            return False
    rows, state = inputs['tracks'], inputs['state']
    if any(row['video_id'] not in videos or (not row['is_full'] and row['position'] is not None) for row in rows):
        return False
    full = [row for row in rows if row['is_full']]
    if full and (any(row['position'] is None for row in full) or
                 [row['video_id'] for row in full] != videos):
        return False
    if full and (not state or not state['complete'] or state['expected_track_count'] != len(full)):
        return False
    return not (not full and state and state['complete'] and state['expected_track_count'])
