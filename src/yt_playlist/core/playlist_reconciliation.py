"""Verify Music's filtered playlist against complete YouTube occurrence membership."""
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.youtube_playlist_report import youtube_playlist_snapshot
from yt_playlist.util.duration import parse_duration
from ytmusicapi.exceptions import YTMusicError


def _positions(detail):
    return [(track.get('setVideoId'), track.get('videoId')) for track in detail['tracks']]


def _text(value):
    if not isinstance(value, dict):
        return ''
    if isinstance(value.get('simpleText'), str):
        return value['simpleText']
    runs = value.get('runs', [])
    if not isinstance(runs, list):
        return ''
    return ''.join(run['text'] for run in runs
                   if isinstance(run, dict) and isinstance(run.get('text'), str))


def _hidden_track(video):
    # These are YouTube's display fields, not inferred Music ratings or album
    # identities. A missing rating must continue to block neutral reconciliation.
    byline = video.get('shortBylineText') or video.get('longBylineText')
    track = {'videoId': video['videoId'], 'setVideoId': video['setVideoId'],
             'title': _text(video.get('title')), 'artists': [{'name': _text(byline)}],
             'duration_seconds': parse_duration(video.get('lengthSeconds') or
                                                _text(video.get('lengthText')))}
    thumbnail = video.get('thumbnail')
    if isinstance(thumbnail, dict) and isinstance(thumbnail.get('thumbnails'), list):
        track['thumbnails'] = thumbnail['thumbnails']
    if video.get('isPlayable') is False:
        track['isAvailable'] = False
    return track


def reconcile_playlist(client, playlist_id, detail, read_music):
    """Fill proven missing occurrences; leave every ambiguous read partial.

    The Music count must equal the exhausted YouTube occurrence count, both
    responses must identify the requested playlist, and Music's unique handles
    must form an ordered subset. A second Music read brackets the YouTube read
    to reject concurrent membership/order changes. Preserve Music's alternate
    playback IDs and ratings for shared occurrences, including repeated songs.
    """
    count, tracks, audit = detail.get('trackCount'), detail['tracks'], detail['_readAudit']
    if (not playlist_id.startswith('PL') or detail.get('id') != playlist_id
            or type(count) is not int or count <= len(tracks)
            or audit.get('exhausted') is not True or audit.get('omitted')
            or audit.get('unidentified') or audit.get('raw_items') != len(tracks)):
        return detail
    positions = _positions(detail)
    handles = [handle for handle, _ in positions]
    if any(not isinstance(handle, str) or not handle for handle in handles):
        audit['reconciliation'] = 'Music occurrence handles unavailable'
        return detail
    handle_set = set(handles)
    if len(handle_set) != len(handles):
        audit['reconciliation'] = 'Music occurrence handles unavailable'
        return detail
    try:
        youtube = youtube_playlist_snapshot(client, playlist_id)
        rows = youtube['tracks']
        remote_handles = [row.get('setVideoId') for row in rows]
        if (not youtube['playlist_id_verified'] or len(rows) != count
                or any(not isinstance(handle, str) or not handle for handle in remote_handles)
                or len(set(remote_handles)) != len(remote_handles)
                or [handle for handle in remote_handles if handle in handle_set] != handles):
            audit['reconciliation'] = 'YouTube membership did not verify Music occurrences'
            return detail
        latest = read_music()
        if (latest.get('id') != playlist_id or latest.get('trackCount') != count
                or _positions(latest) != positions or latest['_readAudit'] != audit):
            # A later complete Music response is useful on its own; otherwise
            # the ordinary completeness guard still preserves cached membership.
            latest['_readAudit']['reconciliation'] = 'Music membership changed during verification'
            return latest
        by_handle = {track['setVideoId']: track for track in latest['tracks']}
        merged = [by_handle.get(row['setVideoId']) or _hidden_track(row) for row in rows]
    except (LibrarySnapshotError, BridgeError, YTMusicError, OSError):
        audit['reconciliation'] = 'YouTube membership verification unavailable'
        return detail
    return {**latest, 'tracks': merged,
            '_readAudit': {**latest['_readAudit'], 'pages': youtube['pages'],
                           'raw_items': len(rows), 'source': 'youtube-verified-occurrences',
                           'music_rows': len(tracks), 'hidden_rows': len(rows) - len(tracks)}}
