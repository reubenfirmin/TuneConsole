"""Read YouTube's unavailable-inclusive playlist view through the account bridge.

Raw rows stay inside the reader. The support report exposes bounded aggregates;
Music reconciliation separately verifies playlist identity and occurrence handles.
The WEB context and unavailable browse option follow yt-dlp's implementation.
"""
from copy import deepcopy

from ytmusicapi import YTMusic
from ytmusicapi.continuations import CONTINUATION_ITEMS, get_continuation_token
from ytmusicapi.exceptions import YTMusicError
from ytmusicapi.navigation import nav

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.album_snapshot import LibrarySnapshotError, _check_response
from yt_playlist.core.bridge import BridgeError


class _YoutubeRead:
    def __init__(self, client):
        self.client = client
        self.context = deepcopy(client.context)
        self.context['context']['client'].update(clientName='WEB', clientVersion='2.20260708.00.00')

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, body):
        check_stopping()
        return YTMusic._send_request(self, 'browse', body)


def _first_items(response, playlist_id):
    pending, shelves, visited = [response.get('contents')], [], 0
    while pending:
        value = pending.pop()
        visited += 1
        if visited > 10000:
            raise ValueError
        if isinstance(value, dict):
            if 'playlistVideoListRenderer' in value:
                shelves.append(value['playlistVideoListRenderer'])
            else:
                pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if len(shelves) != 1:
        raise ValueError
    shelf = shelves[0]
    if shelf.get('continuations') or shelf.get('playlistId') not in (None, playlist_id):
        raise ValueError
    return shelf['contents'], shelf.get('playlistId') == playlist_id


def youtube_playlist_snapshot(client, playlist_id, *, include_unavailable=True):
    """Return every identified occurrence, or reject the entire read.

    A diagnostic can inspect a response without a playlist ID. Reconciliation
    additionally requires ``playlist_id_verified`` before using its membership.
    """
    reader = _YoutubeRead(client)
    body = {'browseId': 'VL' + playlist_id}
    if include_unavailable:
        body['params'] = 'wgYCCAA='
    tracks, seen, handles = [], set(), set()
    verified = False
    for page in range(1, 101):
        response = reader.request(body)
        _check_response(response, 'youtube-playlist', page)
        try:
            if page == 1:
                contents, verified = _first_items(response, playlist_id)
            else:
                contents = nav(response, CONTINUATION_ITEMS)
            if not isinstance(contents, list):
                raise ValueError
            for i, item in enumerate(contents):
                if not isinstance(item, dict):
                    raise ValueError
                if 'continuationItemRenderer' in item:
                    token = get_continuation_token([item])
                    if i != len(contents) - 1 or not isinstance(token, str) or not token:
                        raise ValueError
                    continue
                video = item['playlistVideoRenderer']
                if not isinstance(video, dict):
                    raise ValueError
                video_id, handle = video.get('videoId'), video.get('setVideoId')
                if not isinstance(video_id, str) or not video_id.strip():
                    raise ValueError
                if handle is not None:
                    if not isinstance(handle, str) or not handle or handle in handles:
                        raise ValueError
                    handles.add(handle)
                tracks.append(video)
            token = get_continuation_token(contents) if contents else None
        except (KeyError, TypeError, IndexError, ValueError):
            raise LibrarySnapshotError('youtube-playlist', 'unsupported or incomplete track page',
                                       response, page) from None
        if not token:
            return {'tracks': tracks, 'pages': page, 'playlist_id_verified': verified}
        if token in seen:
            raise LibrarySnapshotError('youtube-playlist', 'repeated continuation', {}, page)
        seen.add(token)
        body = {'continuation': token}
    raise LibrarySnapshotError('youtube-playlist', 'page limit exceeded', {}, 100)


def _hidden_music_metadata(client, videos):
    """Summarize Music's classification of hidden entries, without private metadata."""
    if len(videos) > 10:
        return {'inspected': 0, 'limited': True}
    statuses, types, categories = {}, {}, {}
    for video_id in sorted(videos):
        check_stopping()
        try:
            song = client.get_song(video_id)
        except (BridgeError, YTMusicError, OSError, ValueError):
            statuses['REQUEST_FAILED'] = statuses.get('REQUEST_FAILED', 0) + 1
            continue
        status = song.get('playabilityStatus', {}).get('status')
        status = status if status in {'OK', 'UNPLAYABLE', 'ERROR', 'LOGIN_REQUIRED',
                                     'AGE_CHECK_REQUIRED', 'CONTENT_CHECK_REQUIRED',
                                     'LIVE_STREAM_OFFLINE'} else 'OTHER'
        statuses[status] = statuses.get(status, 0) + 1
        kind = song.get('videoDetails', {}).get('musicVideoType')
        kind = kind if kind in {'MUSIC_VIDEO_TYPE_ATV', 'MUSIC_VIDEO_TYPE_OMV', 'MUSIC_VIDEO_TYPE_UGC',
                               'MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK',
                               'MUSIC_VIDEO_TYPE_PODCAST_EPISODE'} else 'OTHER'
        types[kind] = types.get(kind, 0) + 1
        microformat = song.get('microformat') or {}
        metadata = microformat.get('microformatDataRenderer') or microformat.get('playerMicroformatRenderer') or {}
        category = 'music' if metadata.get('category') == 'Music' else 'other_or_unknown'
        categories[category] = categories.get(category, 0) + 1
    return {'inspected': len(videos), 'playability': statuses, 'music_video_types': types,
            'categories': categories}


def youtube_playlist_report(client, playlist_id, music_tracks, *, include_unavailable=True):
    music_ids = {t['videoId'] for t in music_tracks if t.get('videoId')}
    music_handles = {t['setVideoId']: t['videoId'] for t in music_tracks
                     if t.get('setVideoId') and t.get('videoId')}
    try:
        snapshot = youtube_playlist_snapshot(client, playlist_id,
                                              include_unavailable=include_unavailable)
    except LibrarySnapshotError as exc:
        return {'error': str(exc), 'exhausted': False}
    except (BridgeError, YTMusicError, OSError):
        return {'error': 'YouTube playlist request unavailable', 'exhausted': False}
    tracks = snapshot['tracks']
    videos = {t['videoId'] for t in tracks}
    unavailable = {t['videoId'] for t in tracks if t.get('isPlayable') is False}
    handles = {t['setVideoId']: t['videoId'] for t in tracks if t.get('setVideoId')}
    return {'pages': snapshot['pages'], 'rows': len(tracks), 'unidentified': 0,
            'playlist_id_verified': snapshot['playlist_id_verified'],
            'unavailable_video_ids': len(unavailable), 'exhausted': True,
            'additional_video_ids': len(videos - music_ids),
            'additional_unavailable_video_ids': len(unavailable - music_ids),
            'missing_music_video_ids': len(music_ids - videos),
            'playlist_handles': len(handles), 'music_handles': len(music_handles),
            'shared_playlist_handles': len(handles.keys() & music_handles.keys()),
            'substituted_video_positions': sum(
                handles[key] != music_handles[key] for key in handles.keys() & music_handles.keys()),
            'additional_playlist_positions': len(handles.keys() - music_handles.keys()),
            'hidden_music_metadata': _hidden_music_metadata(client, videos - music_ids)}
