"""Account for every playlist page without treating parser output as pagination proof."""
from copy import deepcopy
import json

from ytmusicapi import YTMusic
from ytmusicapi.continuations import CONTINUATION_ITEMS, get_continuation_token
from ytmusicapi.exceptions import YTMusicError
from ytmusicapi.navigation import SECTION, TWO_COLUMN_RENDERER, nav
from ytmusicapi.parsers.playlists import parse_playlist_items

from yt_playlist.core.album_snapshot import LibrarySnapshotError, _check_response, response_shape
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.bridge import BridgeError


class _PlaylistRead:
    """A per-call adapter; never replace methods or state on a shared account client."""

    def __init__(self, client, *, browse_params=None, collect_identities=False):
        self.client = client
        self.tracks = []
        self.pages = self.raw_items = self.omitted = self.unidentified = 0
        self.pending = None
        self.requested = set()
        self.collaborative = False
        self.empty_header = False
        self.browse_params = browse_params
        self.first_shape = None
        self.message_paths = []
        self.validation_stage = 'request'
        self.item_index = None
        self.failed_page = None
        self.identity_rows = [] if collect_identities else None

    def _page_error(self, reason, response):
        self.failed_page = {'page': self.pages + 1, 'stage': self.validation_stage,
                            'envelope': _page_envelope(response)}
        if self.item_index is not None:
            self.failed_page['item_index'] = min(self.item_index, 1000)
        details = json.dumps(self.failed_page, sort_keys=True, separators=(',', ':'))
        return LibrarySnapshotError('playlist', f'{reason}; diagnostics={details}', response, self.pages + 1)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _send_request(self, endpoint, body, additionalParams=''):
        check_stopping()
        self.validation_stage = 'continuation.request' if self.pages else 'initial.request'
        self.item_index = None
        if not self.pages and self.browse_params:
            body = {**body, 'params': self.browse_params}
        token = body.get('continuation')
        if self.pages:
            if token != self.pending or token in self.requested:
                raise self._page_error('repeated or unexpected continuation', {})
            self.requested.add(token)
        response = self.client._send_request(endpoint, body, additionalParams)
        if not self.pages:
            self.first_shape = response_shape(response)
            self.message_paths = _message_paths(response)
        self.validation_stage = 'response.status'
        try:
            _check_response(response, 'playlist', self.pages + 1)
        except LibrarySnapshotError as exc:
            raise self._page_error(exc.reason, response) from None
        header_count = 'unknown'
        try:
            if not self.pages:
                self.validation_stage = 'initial.section'
                section = nav(response, [*TWO_COLUMN_RENDERER, 'secondaryContents', *SECTION])
                self.validation_stage = 'initial.shelves'
                shelves = [item['musicPlaylistShelfRenderer'] for item in section['contents']
                           if isinstance(item, dict) and 'musicPlaylistShelfRenderer' in item]
                if len(shelves) != 1:
                    raise ValueError
                if shelves[0].get('continuations'):
                    raise ValueError  # Do not certify an unfollowed legacy continuation.
                # The same flag used by the upstream parser for collaborative columns.
                self.validation_stage = 'initial.header'
                metadata = _header_metadata(response)
                self.collaborative = 'collaborators' in metadata
                count = metadata.get('trackCount')
                header_count = count if type(count) is int else 'unknown'
                self.empty_header = type(count) is int and count == 0
                shelf = shelves[0]
                self.validation_stage = 'initial.contents'
                empty_state = shelf.get('emptyState')
                if ('contents' not in shelf and self.empty_header
                        and (isinstance(shelf.get('messageRenderer'), dict)
                             or isinstance(empty_state, dict)
                             and isinstance(empty_state.get('messageRenderer'), dict)
                             or any(isinstance(item, dict) and set(item) == {'messageRenderer'}
                                    for item in section['contents']))):
                    # Some empty lists put the message beside the empty track
                    # shelf. Both the zero count and explicit message are needed.
                    contents = []
                else:
                    contents = shelf['contents']
            else:
                self.validation_stage = 'continuation.envelope'
                contents = nav(response, CONTINUATION_ITEMS)
            self._append(contents)
        except (KeyError, TypeError, IndexError, ValueError):
            raise self._page_error(f'unsupported or incomplete track page (header_count={header_count})',
                                   response) from None
        self.pages += 1
        if self.pages == 1 and contents == []:
            # Upstream indexes contents[-1] even for a genuinely empty shelf.
            # Its metadata-only branch can parse the same response safely.
            response = deepcopy(response)
            section = nav(response, [*TWO_COLUMN_RENDERER, 'secondaryContents', *SECTION])
            for item in section['contents']:
                if 'musicPlaylistShelfRenderer' in item:
                    item['musicPlaylistShelfRenderer'].pop('contents', None)
        return response

    def _append(self, contents):
        self.validation_stage = 'contents.type'
        if not isinstance(contents, list):
            raise ValueError
        for i, item in enumerate(contents):
            self.item_index = i
            self.validation_stage = 'contents.item_type'
            if not isinstance(item, dict):
                raise ValueError
            if 'continuationItemRenderer' in item:
                self.validation_stage = 'contents.continuation'
                token = get_continuation_token([item])
                if i != len(contents) - 1 or not isinstance(token, str) or not token:
                    raise ValueError
            elif 'musicResponsiveListItemRenderer' not in item:
                self.validation_stage = 'contents.renderer'
                # Music renders an empty saved list as a message in its track
                # shelf. Require the independent explicit zero header, a sole
                # message, and the first page; notices on a nonempty list are
                # never treated as evidence of missing membership.
                if not (self.pages == 0 and self.empty_header and len(contents) == 1
                        and set(item) == {'messageRenderer'}):
                    raise ValueError
        self.item_index = None
        self.validation_stage = 'contents.parse'
        parsed = parse_playlist_items(contents, is_collaborative=self.collaborative)
        rows = sum('musicResponsiveListItemRenderer' in item for item in contents)
        if self.identity_rows is not None and rows == len(parsed):
            raw_rows = [item['musicResponsiveListItemRenderer'] for item in contents
                        if 'musicResponsiveListItemRenderer' in item]
            self.identity_rows.extend(_item_identities(raw, track) for raw, track in zip(raw_rows, parsed))
        self.raw_items += rows
        self.omitted += rows - len(parsed)
        self.unidentified += sum(not t.get('videoId') for t in parsed)
        self.tracks.extend(parsed)
        self.validation_stage = 'contents.next_token'
        self.pending = get_continuation_token(contents) if contents else None


def _item_identities(raw, track):
    """Keep known item/playback identities in memory for the read-only report."""
    paths = {
        'playlist_item': ['playlistItemData', 'videoId'],
        'like_target': ['menu', 'menuRenderer', 'topLevelButtons', 0,
                        'likeButtonRenderer', 'target', 'videoId'],
        'title_watch': ['flexColumns', 0, 'musicResponsiveListItemFlexColumnRenderer',
                        'text', 'runs', 0, 'navigationEndpoint', 'watchEndpoint', 'videoId'],
    }
    result = {'playback': track.get('videoId')}
    for name, path in paths.items():
        value = nav(raw, path, True)
        result[name] = value if isinstance(value, str) and value.strip() else None
    return result


def _page_envelope(response):
    """Fixed paths, types and capped counts only; never echo keys, values or tokens."""
    def kind(value):
        if isinstance(value, dict):
            return f'object[{min(len(value), 1000)}]'
        if isinstance(value, list):
            return f'list[{min(len(value), 1000)}]'
        return ('null' if value is None else 'boolean' if isinstance(value, bool) else
                'string' if isinstance(value, str) else 'number' if isinstance(value, (int, float)) else 'other')

    result = {'root': kind(response)}
    if not isinstance(response, dict):
        return result
    for name in ('responseContext', 'contents', 'continuationContents', 'onResponseReceivedActions',
                 'onResponseReceivedEndpoints'):
        result[name] = kind(response[name]) if name in response else 'missing'
    for name in ('onResponseReceivedActions', 'onResponseReceivedEndpoints'):
        actions = response.get(name)
        if not isinstance(actions, list):
            continue
        samples = []
        for action in actions[:8]:
            sample = {'type': kind(action)}
            if isinstance(action, dict):
                append = action.get('appendContinuationItemsAction')
                sample['append'] = kind(append) if 'appendContinuationItemsAction' in action else 'missing'
                if isinstance(append, dict):
                    items = append.get('continuationItems')
                    sample['items'] = kind(items) if 'continuationItems' in append else 'missing'
                    if isinstance(items, list):
                        sample['sampled_items'] = [
                            'track' if isinstance(item, dict) and 'musicResponsiveListItemRenderer' in item else
                            'continuation' if isinstance(item, dict) and 'continuationItemRenderer' in item else
                            kind(item) for item in items[:8]]
            samples.append(sample)
        result[name + '.sample'] = samples
    continuation = response.get('continuationContents')
    if isinstance(continuation, dict):
        for name in ('musicPlaylistShelfContinuation', 'musicShelfContinuation', 'sectionListContinuation'):
            if name in continuation:
                result['continuationContents.' + name] = kind(continuation[name])
    return result


def _header_metadata(response):
    """Inspect only the known playlist header, without logging its private contents."""
    from ytmusicapi.navigation import (EDITABLE_PLAYLIST_DETAIL_HEADER, HEADER,
                                      RESPONSIVE_HEADER, SECTION_LIST_ITEM, TAB_CONTENT)
    header = nav(response, [*TWO_COLUMN_RENDERER, *TAB_CONTENT, *SECTION_LIST_ITEM], True)
    if header is None:  # Audio playlists can have a shelf without a detail header.
        return {}
    if EDITABLE_PLAYLIST_DETAIL_HEADER[0] in header:
        header = nav(header, [*EDITABLE_PLAYLIST_DETAIL_HEADER, *HEADER, *RESPONSIVE_HEADER])
    else:
        header = nav(header, RESPONSIVE_HEADER)
    from ytmusicapi.parsers.playlists import parse_playlist_header_meta
    return parse_playlist_header_meta(header)


def complete_playlist(client, playlist_id):
    liked_music = playlist_id in ('LM', 'VLLM')
    reader = _PlaylistRead(client, collect_identities=liked_music)
    detail = _complete_playlist(reader, playlist_id)
    if liked_music:
        from yt_playlist.core.liked_music_snapshot import reconcile_liked_music

        def read_music():
            latest = _PlaylistRead(client, collect_identities=True)
            return _complete_playlist(latest, playlist_id), latest.identity_rows

        return reconcile_liked_music(client, detail, reader.identity_rows, read_music)
    from yt_playlist.core.playlist_reconciliation import reconcile_playlist
    return reconcile_playlist(client, playlist_id, detail,
                              lambda: _complete_playlist(_PlaylistRead(client), playlist_id))


def _message_paths(response):
    """Locate empty messages using fixed schema names, without returning their text."""
    fields = {'contents', 'content', 'tabs', 'tabRenderer', 'header', 'primaryContents',
              'secondaryContents', 'twoColumnBrowseResultsRenderer', 'sectionListRenderer',
              'itemSectionRenderer', 'musicPlaylistShelfRenderer', 'emptyState',
              'emptyStateRenderer', 'musicPlaylistEmptyStateRenderer', 'messageRenderer'}
    pending, found, visited = [(response, '', 0)], set(), 0
    while pending and visited < 4096:
        value, path, depth = pending.pop()
        visited += 1
        if depth > 16:
            continue
        if isinstance(value, dict):
            for key, child in list(value.items())[:32]:
                part = key if key in fields else '*'
                child_path = f'{path}.{part}' if path else part
                if key == 'messageRenderer':
                    found.add(child_path)
                elif isinstance(child, (dict, list)):
                    pending.append((child, child_path, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, path + '[]', depth + 1) for child in value[:8])
    return sorted(found)[:16]


def _complete_playlist(reader, playlist_id):
    # Retain upstream metadata parsing and the exact account/request context.
    try:
        detail = YTMusic.get_playlist(reader, playlist_id, limit=None)
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        if isinstance(exc, LibrarySnapshotError):
            raise
        raise LibrarySnapshotError('playlist', 'unsupported metadata or item layout',
                                   {}, max(1, reader.pages)) from None
    # Upstream stops when a page yields no parsed songs, even if it still has a
    # continuation (for example an entire page of deleted entries).
    while reader.pending:
        reader._send_request('browse', {'continuation': reader.pending})
    detail['tracks'] = reader.tracks
    detail['_readAudit'] = {
        'pages': reader.pages, 'raw_items': reader.raw_items, 'omitted': reader.omitted,
        'unidentified': reader.unidentified, 'exhausted': True,
    }
    return detail


def playlist_read_report(client, playlist_id):
    """Compare two read-only browse responses without returning private rows or tokens.

    yt-dlp's YouTube playlist reader uses wgYCCAA= to include unavailable videos.
    This diagnostic checks whether Music honors the same browse option. An
    unchanged response is inconclusive and never relaxes membership validation.
    """
    reports, memberships, ordinary_tracks, resolved_id = {}, {}, [], None
    identity_rows = []
    for name, params in (('ordinary', None), ('include_unavailable_probe', 'wgYCCAA=')):
        reader = _PlaylistRead(client, browse_params=params, collect_identities=True)
        try:
            detail = _complete_playlist(reader, playlist_id)
        except LibrarySnapshotError as exc:
            reports[name] = {'error': str(exc), 'shape': reader.first_shape,
                             'message_paths': reader.message_paths, 'failed_page': reader.failed_page}
            continue
        except (BridgeError, YTMusicError, OSError):
            reports[name] = {'error': 'Playlist request unavailable', 'exhausted': False}
            continue
        tracks = detail['tracks']
        if name == 'ordinary':
            ordinary_tracks = tracks
            resolved_id = detail.get('id')
            identity_rows = reader.identity_rows
        memberships[name] = {t['videoId'] for t in tracks if t.get('videoId')}
        reports[name] = {'advertised': detail.get('trackCount'), 'returned': len(tracks),
                         'unavailable': sum(t.get('isAvailable') is False for t in tracks),
                         'audit': detail['_readAudit'], 'shape': reader.first_shape}
    if len(memberships) == 2:
        ordinary, alternate = memberships.values()
        reports['comparison'] = {'additional_video_ids': len(alternate - ordinary),
                                 'missing_video_ids': len(ordinary - alternate)}
    try:
        production = client.get_playlist(playlist_id, limit=None)
        reports['production'] = {
            'advertised': production.get('trackCount'), 'returned': len(production['tracks']),
            'playlist_id_matches': production.get('id') == playlist_id,
            'playlist_id_kind': ('missing' if not production.get('id') else
                                 'liked-music' if production['id'].startswith('LM') else
                                 'liked-videos' if production['id'].startswith('LL') else
                                 'radio' if production['id'].startswith('RD') else
                                 'ordinary' if production['id'].startswith('PL') else 'other'),
            'unknown_rating_rows': sum(track.get('likeStatus') not in
                                       {'LIKE', 'DISLIKE', 'INDIFFERENT'}
                                       for track in production['tracks']),
            'audit': production.get('_readAudit'),
        }
    except LibrarySnapshotError as exc:
        reports['production'] = {'error': str(exc), 'exhausted': False}
    except (BridgeError, YTMusicError, OSError):
        reports['production'] = {'error': 'Playlist request unavailable', 'exhausted': False}
    if playlist_id.startswith('PL') or playlist_id == 'LM':
        # YouTube's ordinary playlist view exposes entries which Music can hide.
        # The same extension session supplies authentication; no cookies leave it.
        from yt_playlist.core.youtube_playlist_report import youtube_playlist_report
        reports['youtube'] = youtube_playlist_report(client, 'LL' if playlist_id == 'LM' else playlist_id,
                                                     ordinary_tracks)
        if playlist_id == 'LM':
            reports['youtube_music_alias'] = youtube_playlist_report(
                client, playlist_id, ordinary_tracks, music_identity_rows=identity_rows)
            reports['youtube_default'] = youtube_playlist_report(client, 'LL', ordinary_tracks,
                                                                 include_unavailable=False)
            reports['youtube_music_default'] = youtube_playlist_report(client, 'LM', ordinary_tracks,
                                                                       include_unavailable=False)
            if (isinstance(resolved_id, str) and resolved_id.startswith(('LM', 'LL', 'PL'))
                    and resolved_id not in {'LM', 'LL'}):
                reports['youtube_resolved_id'] = youtube_playlist_report(client, resolved_id,
                                                                        ordinary_tracks)
    return reports
