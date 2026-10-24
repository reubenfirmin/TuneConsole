"""Read YouTube's unavailable-inclusive playlist view through the account bridge.

Raw rows stay inside the reader. The support report exposes bounded aggregates;
Music reconciliation separately verifies playlist identity and occurrence handles.
The WEB context and unavailable browse option follow yt-dlp's implementation.
"""
from copy import deepcopy

from ytmusicapi import YTMusic
from ytmusicapi.continuations import get_continuation_token
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
    # Modern video rows carry large thumbnail/menu trees. Inspect the selected
    # playlist container before the bounded legacy search can exhaust its budget
    # traversing those unrelated details.
    try:
        contents = _selected_items(response)
    except (KeyError, TypeError, IndexError, ValueError):
        contents = None
    if isinstance(contents, list) and any(
            isinstance(item, dict) and 'lockupViewModel' in item for item in contents):
        return _lockup_items(response, playlist_id)
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
    if not shelves:
        return _lockup_items(response, playlist_id)
    if len(shelves) != 1:
        raise ValueError
    shelf = shelves[0]
    if shelf.get('continuations') or shelf.get('playlistId') not in (None, playlist_id):
        raise ValueError
    return shelf['contents'], shelf.get('playlistId') == playlist_id


def _lockup_items(response, playlist_id):
    """Read the modern selected playlist tab, never a sidebar or recommendation grid."""
    header = nav(response, ['header', 'playlistHeaderRenderer'], True)
    if not isinstance(header, dict) or header.get('playlistId') != playlist_id:
        raise ValueError
    return _selected_items(response), True


def _selected_items(response):
    tabs = nav(response, ['contents', 'twoColumnBrowseResultsRenderer', 'tabs'])
    if not isinstance(tabs, list):
        raise ValueError
    selected = [tab['tabRenderer'] for tab in tabs if isinstance(tab, dict)
                and isinstance(tab.get('tabRenderer'), dict)
                and (tab['tabRenderer'].get('selected') is True or len(tabs) == 1)]
    if len(selected) != 1:
        raise ValueError
    sections = nav(selected[0], ['content', 'sectionListRenderer', 'contents'])
    if not isinstance(sections, list) or len(sections) != 1:
        raise ValueError
    contents = nav(sections[0], ['itemSectionRenderer', 'contents'])
    if not isinstance(contents, list):
        raise ValueError
    return contents


def _lockup_video(view, playlist_id):
    """Require both the video identity and the exact playlist navigation context."""
    if not isinstance(view, dict) or view.get('contentType') != 'LOCKUP_CONTENT_TYPE_VIDEO':
        raise ValueError
    video_id = view.get('contentId')
    endpoint = nav(view, ['rendererContext', 'commandContext', 'onTap',
                          'innertubeCommand', 'watchEndpoint'])
    if (not isinstance(video_id, str) or not video_id.strip()
            or not isinstance(endpoint, dict) or endpoint.get('videoId') != video_id
            or endpoint.get('playlistId') != playlist_id):
        raise ValueError
    result = {'videoId': video_id}
    try:
        title = nav(view, ['metadata', 'lockupMetadataViewModel', 'title', 'content'], True)
    except TypeError:
        title = None  # Optional display metadata must not invalidate verified membership.
    if isinstance(title, str):
        result['title'] = {'simpleText': title}
    return result


def _continuation_items(response):
    lists = []
    for field in ('onResponseReceivedActions', 'onResponseReceivedEndpoints'):
        actions = response.get(field, [])
        if not isinstance(actions, list):
            raise ValueError
        for action in actions:
            if isinstance(action, dict) and 'appendContinuationItemsAction' in action:
                lists.append(nav(action, ['appendContinuationItemsAction', 'continuationItems']))
    if len(lists) != 1 or not isinstance(lists[0], list):
        raise ValueError
    return lists[0]


def _continuation_token(item):
    legacy = 'continuationItemRenderer' in item
    modern = 'continuationItemViewModel' in item
    if legacy and modern:
        raise ValueError
    if legacy:
        token = get_continuation_token([item])
    elif modern:
        token = nav(item, ['continuationItemViewModel', 'continuationCommand',
                           'innertubeCommand', 'continuationCommand', 'token'])
    else:
        return None
    if not isinstance(token, str) or not token:
        raise ValueError
    return token


def _layout_probe(response, playlist_id, *, failed_item=None):
    """Bounded known-field structure and identity comparisons, without private values."""
    fields = {'playlistId', 'videoId', 'contentId', 'contentType', 'rendererContext',
              'commandContext', 'onTap', 'innertubeCommand', 'watchEndpoint',
              'navigationEndpoint', 'browseEndpoint', 'command', 'commands'}
    header = nav(response, ['header', 'playlistHeaderRenderer'], True)
    sections = nav(response, ['contents', 'twoColumnBrowseResultsRenderer', 'tabs', 0,
                              'tabRenderer', 'content', 'sectionListRenderer', 'contents'], True)
    result = {'header_id_present': isinstance(header, dict) and 'playlistId' in header,
              'header_id_matches': isinstance(header, dict) and header.get('playlistId') == playlist_id,
              'sections': min(len(sections), 1000) if isinstance(sections, list) else None}
    rows = nav(sections, [0, 'itemSectionRenderer', 'contents'], True)
    if isinstance(rows, list):
        result['rows'] = min(len(rows), 1000)
    elif failed_item is None:
        return result
    view = (failed_item.get('lockupViewModel') if isinstance(failed_item, dict)
            else nav(rows, [0, 'lockupViewModel'], True))
    pending, structure = [(view, '', 0)], {}
    content_id = view.get('contentId') if isinstance(view, dict) else None
    while pending and len(structure) < 64:
        value, path, depth = pending.pop()
        if depth > 8:
            continue
        if isinstance(value, dict):
            for key in sorted(fields & value.keys()):
                child, child_path = value[key], path + '.' + key
                structure[child_path] = ('object' if isinstance(child, dict) else
                                         'list' if isinstance(child, list) else
                                         'string' if isinstance(child, str) else 'other')
                if key == 'playlistId':
                    structure[child_path + '.matches_request'] = child == playlist_id
                elif key == 'videoId':
                    structure[child_path + '.matches_content'] = bool(content_id) and child == content_id
                elif key == 'contentType':
                    structure[child_path + '.is_video'] = child == 'LOCKUP_CONTENT_TYPE_VIDEO'
                if isinstance(child, (dict, list)):
                    pending.append((child, child_path, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, path + '[]', depth + 1) for child in value[:2])
    result['failed_row' if failed_item is not None else 'first_row'] = structure
    return result


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
        stage, item_index = 'initial.layout' if page == 1 else 'continuation.layout', None
        try:
            if page == 1:
                contents, verified = _first_items(response, playlist_id)
            else:
                contents = _continuation_items(response)
            if not isinstance(contents, list):
                raise ValueError
            token = None
            for i, item in enumerate(contents):
                stage, item_index = 'contents.row', i
                if not isinstance(item, dict):
                    raise ValueError
                if len(item.keys() & {'lockupViewModel', 'playlistVideoRenderer',
                                      'continuationItemRenderer', 'continuationItemViewModel'}) != 1:
                    raise ValueError
                stage = 'contents.continuation'
                token = _continuation_token(item)
                if token is not None:
                    if i != len(contents) - 1:
                        raise ValueError
                    continue
                stage = 'contents.video'
                video = (_lockup_video(item['lockupViewModel'], playlist_id)
                         if 'lockupViewModel' in item else item['playlistVideoRenderer'])
                if not isinstance(video, dict):
                    raise ValueError
                video_id, handle = video.get('videoId'), video.get('setVideoId')
                if not isinstance(video_id, str) or not video_id.strip():
                    raise ValueError
                if handle is not None:
                    stage = 'contents.handle'
                    if not isinstance(handle, str) or not handle or handle in handles:
                        raise ValueError
                    handles.add(handle)
                tracks.append(video)
            stage, item_index = 'contents.next_token', None
        except (KeyError, TypeError, IndexError, ValueError):
            error = LibrarySnapshotError('youtube-playlist', 'unsupported or incomplete track page',
                                         response, page)
            error.layout_probe = _layout_probe(
                response, playlist_id,
                failed_item=contents[item_index] if item_index is not None else None)
            error.layout_probe['validation_stage'] = stage
            if item_index is not None:
                error.layout_probe['item_index'] = min(item_index, 1000)
            raise error from None
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


def _identity_report(client, rows, music_tracks, videos):
    """Compare explicit original-item IDs without guessing equivalence from titles."""
    sources, candidates = {}, []
    for name in ('playlist_item', 'like_target', 'title_watch'):
        identified = [row for row in rows if row.get(name)]
        ids = {row[name] for row in identified}
        sources[name] = {
            'present_rows': len(identified), 'distinct_video_ids': len(ids),
            'different_playback_rows': sum(row[name] != row.get('playback') for row in identified),
            'additional_video_ids': len(videos - ids), 'missing_video_ids': len(ids - videos),
        }
        if identified and len(identified) == len(rows) == len(music_tracks) and not ids - videos:
            candidates.append((len(videos - ids), name, ids))
    result = {'rows': len(rows), 'sources': sources}
    if candidates:
        _, name, ids = min(candidates, key=lambda candidate: candidate[0])
        result['unmatched_source'] = name
        result['unmatched_music_metadata'] = _hidden_music_metadata(client, videos - ids)
    return result


def youtube_playlist_report(client, playlist_id, music_tracks, *, include_unavailable=True,
                            music_identity_rows=None):
    music_ids = {t['videoId'] for t in music_tracks if t.get('videoId')}
    music_handles = {t['setVideoId']: t['videoId'] for t in music_tracks
                     if t.get('setVideoId') and t.get('videoId')}
    try:
        snapshot = youtube_playlist_snapshot(client, playlist_id,
                                              include_unavailable=include_unavailable)
    except LibrarySnapshotError as exc:
        return {'error': str(exc), 'exhausted': False, 'layout_probe': getattr(exc, 'layout_probe', None)}
    except (BridgeError, YTMusicError, OSError):
        return {'error': 'YouTube playlist request unavailable', 'exhausted': False}
    tracks = snapshot['tracks']
    videos = {t['videoId'] for t in tracks}
    unavailable = {t['videoId'] for t in tracks if t.get('isPlayable') is False}
    handles = {t['setVideoId']: t['videoId'] for t in tracks if t.get('setVideoId')}
    report = {'pages': snapshot['pages'], 'rows': len(tracks), 'unidentified': 0,
            'distinct_video_ids': len(videos), 'music_distinct_video_ids': len(music_ids),
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
    if music_identity_rows is not None:
        report['item_identities'] = _identity_report(client, music_identity_rows, music_tracks, videos)
    return report
