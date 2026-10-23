"""Explicit empty history succeeds; unsupported sections retain bounded diagnostics."""
import json

import pytest

from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core.history_snapshot import HistorySnapshotError
from yt_playlist.library import sync


def response(sections):
    return {'contents': {'singleColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {
        'content': {'sectionListRenderer': {'contents': sections}}}}]}}}


EMPTY_MESSAGE = 'Once you listen to something it will show up here.'


def empty_response(text=None, **wrapper):
    page = response([{'itemSectionRenderer': {'contents': [{'messageRenderer': {
        'text': text or {'runs': [{'text': EMPTY_MESSAGE}]}}}]}}])
    page['contents']['singleColumnBrowseResultsRenderer']['tabs'][0][
        'tabRenderer']['content']['sectionListRenderer'].update(wrapper)
    return page


@pytest.mark.parametrize('wrapper', [{}, {'targetId': 'sanitized-history-target',
                                       'trackingParams': 'sanitized-tracking'}])
@pytest.mark.parametrize('text', [
    {'runs': [{'text': EMPTY_MESSAGE}]},
    {'runs': [{'text': 'Once you listen to something '}, {'text': 'it will show up here.'}]},
    {'simpleText': EMPTY_MESSAGE},
])
def test_explicit_empty_history_completes_through_production_client(text, wrapper):
    client, bridge = _production_client([empty_response(text, **wrapper)])
    assert client.get_history() == []
    assert len(bridge.calls) == 1
    assert bridge.calls[0][1]['browseId'] == 'FEmusic_history'


@pytest.mark.parametrize('target', [None, {}, [], True, 1])
def test_non_string_history_targets_remain_invalid(target):
    client, _ = _production_client([empty_response(targetId=target)])
    with pytest.raises(HistorySnapshotError, match='incomplete empty history layout'):
        client.get_history()


@pytest.mark.parametrize('message', [
    'Something went wrong. Try again later.',
    'Sign in to view your history',
    'History is paused',
    'Unknown upstream message with PRIVATE_PAYLOAD',
    '',
])
def test_unknown_or_error_history_messages_remain_failed_and_redacted(message):
    client, _ = _production_client([empty_response({'runs': [{'text': message}]})])
    with pytest.raises(HistorySnapshotError) as caught:
        client.get_history()
    assert 'unsupported or empty history shelf' in str(caught.value)
    assert 'PRIVATE_PAYLOAD' not in str(caught.value)


@pytest.mark.parametrize('extra', ['continuation', 'other-section', 'button', 'missing-sections'])
def test_empty_message_does_not_hide_incomplete_or_mixed_history(extra):
    page = empty_response()
    section_list = page['contents']['singleColumnBrowseResultsRenderer']['tabs'][0][
        'tabRenderer']['content']['sectionListRenderer']
    if extra == 'continuation':
        section_list['continuations'] = [{'nextContinuationData': {'continuation': 'PRIVATE_TOKEN'}}]
    elif extra == 'other-section':
        section_list['contents'].append({'unknownRenderer': {}})
    elif extra == 'missing-sections':
        section_list['contents'] = []
    else:
        section_list['contents'][0]['itemSectionRenderer']['contents'][0]['messageRenderer'][
            'button'] = {'buttonRenderer': {'text': {'simpleText': 'Try again'}}}
    client, _ = _production_client([page])
    with pytest.raises(HistorySnapshotError) as caught:
        client.get_history()
    assert 'PRIVATE_TOKEN' not in str(caught.value)


def test_empty_wrapper_diagnostic_identifies_schema_types_without_private_values():
    page = empty_response()
    section_list = page['contents']['singleColumnBrowseResultsRenderer']['tabs'][0][
        'tabRenderer']['content']['sectionListRenderer']
    section_list.update({'targetId': 'PRIVATE_TARGET', 'header': {'PRIVATE_HEADER': 'secret'},
                         'disablePullToRefresh': True, 'PRIVATE_FIELD': 'PRIVATE_VALUE'})
    client, _ = _production_client([page])
    with pytest.raises(HistorySnapshotError) as caught:
        client.get_history()
    message = str(caught.value)
    assert 'disablePullToRefresh=boolean,header=object,targetId=string,other_fields[1]' in message
    assert 'PRIVATE' not in message and 'secret' not in message


@pytest.mark.parametrize('auth', ['logged-out', 'sign-in', 'http-error'])
def test_empty_message_cannot_override_authentication_or_upstream_errors(auth):
    page = empty_response()
    if auth == 'logged-out':
        page['responseContext'] = {'mainAppWebResponseContext': {'loggedOut': True}}
    elif auth == 'sign-in':
        message = page['contents']['singleColumnBrowseResultsRenderer']['tabs'][0][
            'tabRenderer']['content']['sectionListRenderer']['contents'][0][
                'itemSectionRenderer']['contents'][0]['messageRenderer']
        message['button'] = {'buttonRenderer': {'navigationEndpoint': {'signInEndpoint': {}}}}
    else:
        page['error'] = {'code': 503, 'message': 'PRIVATE_UPSTREAM_ERROR'}
    client, _ = _production_client([page])
    with pytest.raises(ValueError) as caught:
        client.get_history()
    assert sync._is_auth_error(caught.value) is (auth != 'http-error')
    assert 'PRIVATE_UPSTREAM_ERROR' not in str(caught.value)


@pytest.mark.parametrize('section', [
    {'musicNotifierShelfRenderer': {'private': 'notifier-secret'}},
    {'musicHistoryEmptyStateRenderer': {'private': 'history-secret'}},
    {'musicShelfRenderer': {'contents': []}},
])
def test_missing_history_shelf_does_not_become_a_none_error_or_auth_failure(section):
    client, bridge = _production_client([response([section])])
    with pytest.raises(HistorySnapshotError) as caught:
        client.get_history()
    message = str(caught.value)
    assert 'history snapshot' in message and 'client=ytmusicapi/' in message
    assert 'History in YouTube Music' in message
    assert 'secret' not in message and 'private' not in message
    assert 'YTMusicServerError: None' not in message
    assert not sync._is_auth_error(caught.value)
    assert len(bridge.calls) == 1


def test_explicit_signed_out_history_remains_auth_failure():
    client, _ = _production_client([{'responseContext': {
        'mainAppWebResponseContext': {'loggedOut': True}}}])
    with pytest.raises(ValueError) as caught:
        client.get_history()
    assert sync._is_auth_error(caught.value)


def test_unknown_shelf_items_cannot_be_silently_dropped_as_empty_history():
    client, _ = _production_client([response([{'musicShelfRenderer': {
        'title': {'runs': [{'text': 'Today'}]},
        'contents': [{'unknownRenderer': {'private': 'secret'}}]}}])])
    with pytest.raises(HistorySnapshotError, match='unsupported history shelf item') as caught:
        client.get_history()
    assert 'secret' not in str(caught.value)


def test_supported_history_shelves_keep_upstream_parsing_and_played_date():
    from tests.core.test_playlist_page_accounting import item

    row = item('song')
    row['musicResponsiveListItemRenderer']['overlay'] = {'musicItemThumbnailOverlayRenderer': {
        'content': {'musicPlayButtonRenderer': {'playNavigationEndpoint': {'watchEndpoint': {'videoId': 'song'}}}}}}
    client, bridge = _production_client([response([{'musicShelfRenderer': {
        'title': {'runs': [{'text': 'Today'}]}, 'contents': [row]}}])])
    tracks = client.get_history()
    assert len(tracks) == 1 and tracks[0]['videoId'] == 'song' and tracks[0]['played'] == 'Today'
    assert bridge.calls[0][1]['browseId'] == 'FEmusic_history'


def test_http_error_reports_only_status_without_private_message():
    from ytmusicapi.exceptions import YTMusicServerError

    client, _ = _production_client([YTMusicServerError('Server returned HTTP 503: Error.\nPRIVATE_PAYLOAD')])
    with pytest.raises(HistorySnapshotError) as caught:
        client.get_history()
    assert 'status=503' in str(caught.value) and 'PRIVATE_PAYLOAD' not in str(caught.value)


def test_unsupported_history_preserves_prior_plays_and_success_freshness(store, monkeypatch):
    from tests.test_sync_outcomes import Client

    iid = store.identities.upsert_identity('Main', 'bridge', None, True)
    client = Client()
    monkeypatch.setattr(sync, 'with_retry', lambda fn, **_kw: fn())
    sync.sync_all(store, {iid: client}, 100)
    tid = store.tracks.upsert_track('kept', 'Kept', 'Artist', '', 200)
    key = store.tracks.catalog_subject(tid)['identity_key']
    store.history.record_history_plays(iid, 100, [(key, 'Today')])
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM listening_observations').fetchall()]
    assert before
    production, _ = _production_client([response([{'musicHistoryEmptyStateRenderer': {}}])])
    monkeypatch.setattr(client, 'get_history', production.get_history)
    result = sync.sync_all(store, {iid: client}, 200)
    history = result['accounts'][str(iid)]['resources']['history']
    assert history['status'] == 'failed' and history['last_success_at'] == 100
    assert history['last_attempt_at'] == 200
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM listening_observations').fetchall()] == before
    assert 'unsupported or empty history shelf' in history['message']
    assert json.loads(store.settings.get_setting('library_sync_state')) == result


def test_verified_empty_history_advances_freshness_without_removing_retained_listening(store, monkeypatch):
    from tests.test_sync_outcomes import Client

    iid = store.identities.upsert_identity('Main', 'bridge', None, True)
    client = Client()
    monkeypatch.setattr(sync, 'with_retry', lambda fn, **_kw: fn())
    sync.sync_all(store, {iid: client}, 100)
    tid = store.tracks.upsert_track('kept', 'Kept', 'Artist', '', 200)
    key = store.tracks.catalog_subject(tid)['identity_key']
    store.history.record_history_plays(iid, 100, [(key, 'Today')])
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM listening_observations').fetchall()]
    assert before
    production, bridge = _production_client([empty_response(targetId='sanitized-history-target')])
    monkeypatch.setattr(client, 'get_history', production.get_history)
    result = sync.sync_all(store, {iid: client}, 200)
    history = result['accounts'][str(iid)]['resources']['history']
    assert history['status'] == 'complete' and history['last_success_at'] == 200
    assert history['last_attempt_at'] == 200 and history['refreshed'] == 0
    assert len(bridge.calls) == 1
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM listening_observations').fetchall()] == before
    assert json.loads(store.settings.get_setting('library_sync_state')) == result
