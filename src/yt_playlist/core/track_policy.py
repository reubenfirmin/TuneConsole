"""Recording matching and exact-upload rating decisions."""
from math import inf, nextafter

from yt_playlist.core.rating_feedback import Rating
from yt_playlist.util.matching import normalize, recording_title
from yt_playlist.util.thumbnails import is_placeholder_art


def equivalent_upload(requested, accepted):
    first, second = requested.get('duration'), accepted.get('duration')
    artists = [set(normalize(track.get('artist') or '').split()) for track in (requested, accepted)]
    return bool(requested.get('title') and accepted.get('title')
                and recording_title(requested['title']) == recording_title(accepted['title'])
                and all(artists) and (artists[0] <= artists[1] or artists[1] <= artists[0])
                and type(first) in (int, float) and type(second) in (int, float)
                and first > 0 and second > 0 and abs(first - second) <= 2)


def match_recording(rows, title, artist):
    keys = {row['identity_key'] for row in rows if recording_title(row['title']) == recording_title(title)
            and normalize(row['artist']) == normalize(artist)}
    return next(iter(keys)) if len(keys) == 1 else None


def known_duration(rows, title, artist):
    matching = [row for row in rows
                if (row['title'] or '').strip().casefold() == (title or '').strip().casefold()
                and (row['artist'] or '').strip().casefold() == (artist or '').strip().casefold()]
    return max((row['duration_s'] for row in matching if row['duration_s'] is not None), default=None)


def artwork_update(current, url, *, source, chosen=False):
    """YouTube refreshes real art; other providers only fill missing covers."""
    if chosen or not isinstance(url, str) or not url.strip():
        return None
    useful = current and current.strip() and not is_placeholder_art(current)
    if is_placeholder_art(url) and (source != 'youtube' or current):
        return None
    if source != 'youtube' and useful:
        return None
    return url.strip()


def metadata_edits(current, values):
    edits = []
    for field in ('title', 'artist', 'genre', 'year'):
        if field not in values:
            continue
        value = values[field]
        column = 'mb_year' if field == 'year' else field
        if field in ('title', 'artist') and value == current['orig_' + field]:
            edits.append((column, 'restore', value))
        elif value != current[field]:
            edits.append((column, 'choose', value))
    return edits


def item_rating_changes(account_id, statuses, previous, unbanned, *, source, now):
    observations, actions = [], []
    for video_id, status in statuses.items():
        at = now
        if source == 'api' and status == 'DISLIKE' and video_id in unbanned:
            at = max(now, nextafter(unbanned[video_id], inf))
        prior = previous.get(video_id)
        if source == 'api' and (prior is None or prior['observed_at'] <= at) and (
                prior is None or prior['status'] != status):
            actions.append((account_id, video_id, prior['status'] if prior else None, status, source, at))
        if prior is not None and not (at > prior['observed_at'] or at == prior['observed_at']
                                      and source in ('api', 'action')):
            continue
        retain = source == 'sync' and prior is not None and prior['status'] == status and prior['source'] in ('api', 'action')
        observations.append((account_id, video_id, status, prior['source'] if retain else source,
                             prior['observed_at'] if retain else at))
    return observations, actions


def combined_ratings(rows):
    result = {}
    for row in rows:
        status = row['status']
        if status == 'DISLIKE' and row['unbanned'] and (row['source'] != 'api' or
                row['unbanned_at'] is not None and row['observed_at'] <= row['unbanned_at']):
            status = 'INDIFFERENT'
        candidate = Rating(status, row['observed_at'], 'action' if row['source'] in ('api', 'action') else 'sync')
        key = row['identity_key']
        if key not in result or candidate.priority() > result[key].priority():
            result[key] = candidate
    return result


def pending_rating_projections(rows, combined, feedback_rows):
    feedback = {}
    for row in feedback_rows:
        if row['kind'] in ('like', 'dislike'):
            feedback.setdefault(row['item_key'], {})[row['kind']] = row['reason']
    pending = []
    for row in rows:
        key = row['identity_key']
        if key is None:
            needs_projection = row['status'] in ('LIKE', 'DISLIKE')
        else:
            expected, actual = combined[key], feedback.get(key, {})
            expected_kinds = set() if expected.status == 'INDIFFERENT' else {expected.status.lower()}
            needs_projection = (set(actual) != expected_kinds or expected.status == 'LIKE'
                                and expected.provenance == 'action' and actual.get('like') != 'action')
        if needs_projection:
            pending.append({name: row[name] for name in ('video_id', 'status', 'observed_at')})
    return pending
