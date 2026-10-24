"""Reconcile two scoped Liked Music views by original likes, never playback IDs."""
from ytmusicapi.exceptions import YTMusicError

from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.youtube_playlist_report import youtube_playlist_snapshot


def _music_likes(detail, identities):
    tracks, audit = detail['tracks'], detail['_readAudit']
    if (detail.get('id') not in (None, 'LM', 'VLLM')
            or detail.get('error') or detail.get('continuation') or detail.get('continuations')
            or detail.get('status', 'STATUS_SUCCEEDED') != 'STATUS_SUCCEEDED'
            or type(detail.get('trackCount')) is not int
            or not tracks or audit.get('exhausted') is not True
            or audit.get('omitted') or audit.get('unidentified')
            or audit.get('raw_items') != len(tracks)
            or not isinstance(identities, list) or len(identities) != len(tracks)):
        raise ValueError('Music original likes could not be verified')
    originals = []
    for track, identity in zip(tracks, identities):
        original = identity.get('like_target')
        if (not isinstance(original, str) or not original.strip()
                or not isinstance(track.get('videoId'), str) or not track['videoId'].strip()
                or identity.get('playback') != track['videoId']
                or track.get('likeStatus') != 'LIKE'):
            raise ValueError('Music original likes could not be verified')
        originals.append(original)
    if len(set(originals)) != len(originals):
        raise ValueError('Music original likes are not unique')
    return originals


def _merge_order(music, youtube):
    """A linear merge must have exactly one order consistent with both views.

    A shared original anchors each gap. If both views have different unshared
    originals in the same gap, their relative order is unknown: reject it rather
    than inventing a playlist order. Conflicting shared orders also fail closed.
    """
    music_set, youtube_set = set(music), set(youtube)
    if not music_set & youtube_set:
        raise ValueError('Liked Music views have no shared original likes')
    result, i, j = [], 0, 0
    while i < len(music) or j < len(youtube):
        if i == len(music):
            result.extend(youtube[j:])
            break
        if j == len(youtube):
            result.extend(music[i:])
            break
        if music[i] == youtube[j]:
            result.append(music[i])
            i, j = i + 1, j + 1
        elif music[i] not in youtube_set and youtube[j] not in music_set:
            raise ValueError('Liked Music occurrence order is ambiguous')
        elif music[i] not in youtube_set:
            result.append(music[i])
            i += 1
        elif youtube[j] not in music_set:
            result.append(youtube[j])
            j += 1
        else:
            raise ValueError('Liked Music views disagree on original like order')
    return result


def _partial(detail, reason):
    return {**detail, '_readAudit': {**detail['_readAudit'], 'reconciliation': reason}}


def reconcile_liked_music(client, detail, identities, read_music):
    """Fill only independently witnessed LM originals in a stable Music bracket.

    Both views positively identify their own LM members. Their union must cover
    exactly the advertised count, with unique original like IDs and one compatible
    order. This does not equate YouTube LL with LM or use substitute playback IDs
    as membership identities. Unknown hidden-row ratings stay unknown.
    """
    count = detail.get('trackCount')
    if type(count) is not int or count <= len(detail['tracks']):
        return detail
    try:
        originals = _music_likes(detail, identities)
    except ValueError as exc:
        return _partial(detail, str(exc))
    try:
        snapshot = youtube_playlist_snapshot(client, 'LM')
        rows = snapshot['tracks']
        remote = [row.get('videoId') for row in rows]
        if (snapshot.get('playlist_id_verified') is not True
                or any(not isinstance(value, str) or not value.strip() for value in remote)
                or len(set(remote)) != len(remote)):
            return _partial(detail, 'YouTube LM original likes could not be verified')
        order = _merge_order(originals, remote)
        if len(order) != count:
            return _partial(detail, 'Liked Music union does not match advertised count')
        latest, latest_identities = read_music()
        try:
            latest_originals = _music_likes(latest, latest_identities)
        except ValueError:
            return _partial(latest, 'Music membership changed during verification')
        if (latest.get('id') != detail.get('id') or latest.get('trackCount') != count
                or latest_originals != originals
                or [track['videoId'] for track in latest['tracks']]
                != [track['videoId'] for track in detail['tracks']]
                or latest['_readAudit'] != detail['_readAudit']):
            return _partial(latest, 'Music membership changed during verification')
    except ValueError as exc:
        if isinstance(exc, LibrarySnapshotError):
            return _partial(detail, 'YouTube LM membership verification unavailable')
        # _merge_order raises fixed diagnostics only; request ValueErrors must
        # not echo arbitrary private upstream exception text.
        return _partial(detail, 'Liked Music occurrence order could not be verified')
    except (BridgeError, YTMusicError, OSError):
        return _partial(detail, 'YouTube LM membership verification unavailable')
    by_original = dict(zip(latest_originals, latest['tracks']))
    hidden = {row['videoId']: _hidden_like(row) for row in rows if row['videoId'] not in by_original}
    merged = [by_original[original] if original in by_original else hidden[original]
              for original in order]
    return {**latest, 'tracks': merged,
            '_readAudit': {**latest['_readAudit'], 'raw_items': len(merged),
                           'source': 'music-and-youtube-verified-likes',
                           'music_rows': len(originals), 'youtube_rows': len(rows),
                           'youtube_pages': snapshot['pages'], 'hidden_rows': len(hidden),
                           'music_only_rows': len(set(originals) - set(remote)),
                           'substituted_video_positions': sum(
                               original != track['videoId']
                               for original, track in zip(latest_originals, latest['tracks']))}}


def _hidden_like(row):
    """Retain only explicit display/playability fields; no invented rating/handle."""
    track = {'videoId': row['videoId']}
    title = row.get('title')
    if isinstance(title, dict):
        if isinstance(title.get('simpleText'), str):
            track['title'] = title['simpleText']
        elif isinstance(title.get('runs'), list):
            track['title'] = ''.join(run['text'] for run in title['runs']
                                     if isinstance(run, dict) and isinstance(run.get('text'), str))
    if row.get('isPlayable') is False:
        track['isAvailable'] = False
    return track
