"""History parser diagnostics without retaining titles, account tokens or response bodies."""
import re
from copy import deepcopy
from importlib.metadata import version

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError
from ytmusicapi.navigation import MUSIC_SHELF, SECTION_LIST, SINGLE_COLUMN_TAB, nav

from yt_playlist.core.album_snapshot import LibrarySnapshotError, _check_response, response_shape


def _empty_wrapper_fields(section_list):
    """Describe wrapper metadata without logging values or arbitrary payload keys."""
    schema_fields = {'header', 'footer', 'targetId', 'disablePullToRefresh',
                     'continuations', 'subMenu', 'scrollPaneStyle', 'background',
                     'horizontalScrollMode', 'showBottomSeparator', 'onView'}
    extra = section_list.keys() - {'contents', 'trackingParams'}
    fields = []
    for key in sorted(extra & schema_fields):
        value = section_list[key]
        kind = ('null' if value is None else 'object' if isinstance(value, dict) else
                f'list[{min(len(value), 1000)}]' if isinstance(value, list) else
                'boolean' if isinstance(value, bool) else
                'string' if isinstance(value, str) else 'number' if isinstance(value, (int, float))
                else 'other')
        fields.append(f'{key}={kind}')
    unknown = len(extra - schema_fields)
    if unknown:
        fields.append(f'other_fields[{min(unknown, 1000)}]')
    return ','.join(fields)


def _empty_history(sections):
    """Recognize the English empty state, not an arbitrary upstream notice.

    The client requests English. This message/layout is also recorded in
    Scrobble-Bridge's explicit_empty_history_message_is_a_valid_empty_snapshot
    fixture (o1xhack/Scrobble-Bridge, commit 2e529297d9ed6da4f857cbc3a43bfdbd9e536ac7).
    Unknown/localized messages stay unavailable until their contract is known.
    """
    if len(sections) != 1 or not isinstance(sections[0], dict):
        return False
    section = sections[0]
    if set(section) != {'itemSectionRenderer'}:
        return False
    renderer = section['itemSectionRenderer']
    if not isinstance(renderer, dict) or renderer.keys() - {'contents', 'trackingParams'}:
        return False
    items = renderer.get('contents')
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or set(items[0]) != {'messageRenderer'}):
        return False
    message = items[0]['messageRenderer']
    if not isinstance(message, dict) or message.keys() - {'text', 'trackingParams'}:
        return False
    text = message.get('text')
    if not isinstance(text, dict):
        return False
    if set(text) == {'simpleText'} and isinstance(text['simpleText'], str):
        value = text['simpleText']
    elif (set(text) == {'runs'} and isinstance(text['runs'], list) and text['runs']
          and all(isinstance(run, dict) and set(run) == {'text'} and isinstance(run['text'], str)
                  for run in text['runs'])):
        value = ''.join(run['text'] for run in text['runs'])
    else:
        return False
    return ' '.join(value.split()) == 'Once you listen to something it will show up here.'


class HistorySnapshotError(LibrarySnapshotError):
    def __init__(self, reason, response):
        super().__init__('history', reason, response, 1)
        self.args = (f'history snapshot {reason}; client=ytmusicapi/{version("ytmusicapi")}; '
                     f'page=1; shape={response_shape(response)}; '
                     'check this account\'s History in YouTube Music',)


class _HistoryRead:
    def __init__(self, client):
        self.client = client
        self.response = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _send_request(self, endpoint, body, additionalParams=''):
        try:
            self.response = self.client._send_request(endpoint, body, additionalParams)
        except YTMusicServerError as exc:
            # The upstream message may include private response text. Retain only
            # its fixed HTTP prefix and status, never the arbitrary message.
            match = re.match(r'Server returned HTTP (\d{3}):', str(exc))
            status = int(match[1]) if match else None
            reason = ('signed out' if status in (401, 403) else
                      f'upstream HTTP failure status={status}' if status else 'upstream request failed')
            raise HistorySnapshotError(reason, {}) from None
        _check_response(self.response, 'history', 1)
        try:
            sections = nav(self.response, SINGLE_COLUMN_TAB + SECTION_LIST)
        except (KeyError, TypeError, IndexError):
            raise HistorySnapshotError('unsupported history layout', self.response) from None
        if not isinstance(sections, list) or not sections:
            raise HistorySnapshotError('unavailable history sections', self.response)
        # Notices sit deeper than the bounded full-page shape walk. Check the
        # same auth markers from the section root before interpreting an empty state.
        _check_response({**self.response, 'contents': sections}, 'history', 1)
        if _empty_history(sections):
            section_list = nav(self.response, SINGLE_COLUMN_TAB + SECTION_LIST[:-1])
            # The empty account's real section wrapper includes a targetId
            # string. It names the rendered section; it carries no more rows.
            # Keep rejecting pagination, actions and unknown wrapper fields.
            if (section_list.keys() - {'contents', 'trackingParams', 'targetId'}
                    or ('targetId' in section_list and not isinstance(section_list['targetId'], str))):
                fields = _empty_wrapper_fields(section_list)
                raise HistorySnapshotError(f'incomplete empty history layout ({fields})', self.response)
            # Upstream only understands shelves. Give its parser an empty list
            # after validating the explicit state, without changing the response
            # retained for diagnostics or the shared client's request behavior.
            normalized = deepcopy(self.response)
            nav(normalized, SINGLE_COLUMN_TAB + SECTION_LIST[:-1])['contents'] = []
            return normalized
        for section in sections:
            if not isinstance(section, dict):
                raise HistorySnapshotError('invalid history section', self.response)
            # get_history() otherwise raises YTMusicServerError(None) whenever
            # this section lacks both shelf contents and a notifier title.
            try:
                contents = nav(section, [*MUSIC_SHELF, 'contents'], True)
            except (KeyError, TypeError, IndexError):
                raise HistorySnapshotError('invalid history shelf', self.response) from None
            if not isinstance(contents, list) or not contents:
                reason = ('upstream history notice' if 'musicNotifierShelfRenderer' in section else
                          'unsupported or empty history shelf')
                raise HistorySnapshotError(reason, self.response)
            if any(not isinstance(item, dict) or 'musicResponsiveListItemRenderer' not in item
                   for item in contents):
                raise HistorySnapshotError('unsupported history shelf item', self.response)
        return self.response


def history_snapshot(client):
    reader = _HistoryRead(client)
    try:
        return YTMusic.get_history(reader)
    except LibrarySnapshotError:
        raise
    except (KeyError, TypeError, IndexError, ValueError, YTMusicServerError):
        # Navigation/parser exceptions can embed the entire private payload.
        raise HistorySnapshotError('unsupported history item layout', reader.response) from None
