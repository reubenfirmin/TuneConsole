"""Support reads use the selected account without requiring or modifying cached membership."""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from tests.web_support import _client
from tests.test_saved_album_snapshot import SnapshotBridge
from yt_playlist.core.identities import build_client
from yt_playlist.web.routes import playlists


def test_liked_music_report_without_cached_playlist_selects_explicit_account(store, monkeypatch):
    main = store.accounts.upsert_account('Main', 'main', None, True)
    other = store.accounts.upsert_account('Other', 'other', 'brand', False)
    clients = {main: object(), other: object()}
    calls = []

    def report(client, playlist_id):
        calls.append((client, playlist_id))
        return {'ordinary': {'advertised': 51, 'returned': 47}}

    monkeypatch.setattr(playlists, 'playlist_read_report', report)
    response = _client(store, lambda: clients).get(f'/liked-music/{other}/snapshot-report')

    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {
        'account_id': other, 'reads': {'ordinary': {'advertised': 51, 'returned': 47}}}
    assert calls == [(clients[other], 'LM')]
    assert store.playlists.get_playlists() == []


@pytest.mark.parametrize('configured,connected', [(False, True), (True, False)])
def test_liked_music_report_never_falls_back_to_another_account(store, monkeypatch, configured, connected):
    main = store.accounts.upsert_account('Main', 'main', None, True)
    other = store.accounts.upsert_account('Other', 'other', 'brand', False) if configured else main + 1
    clients = {main: object()}
    if connected:
        clients[other] = object()

    def forbidden(*args):
        pytest.fail('An unavailable account must not issue a remote read')

    monkeypatch.setattr(playlists, 'playlist_read_report', forbidden)
    response = _client(store, lambda: clients).get(f'/liked-music/{other}/snapshot-report')

    assert response.status_code == 409
    assert store.playlists.get_playlists() == []


@pytest.mark.parametrize('wrong_context', [False, True])
def test_saved_deletion_report_uses_owning_account_without_a_local_playlist_or_state_write(store, wrong_context):
    main = store.accounts.upsert_account('Main', 'main', None, True)
    other = store.accounts.upsert_account('Other', 'other', 'PRIVATE_BRAND', False)
    header = {'accountName': {'runs': [{'text': 'PRIVATE_NAME'}]},
              'accountPhoto': {'thumbnails': [{'url': 'https://example.test/PRIVATE_PHOTO'}]}}
    menu = {'actions': [{'openPopupAction': {'popup': {'multiPageMenuRenderer': {
        'header': {'activeAccountHeaderRenderer': header}}}}}]}
    unavailable = {'responseContext': {'mainAppWebResponseContext': {'loggedOut': False}},
                   'trackingParams': 'PRIVATE_TOKEN', 'alerts': [{'alertRenderer': {
                       'type': 'ERROR', 'text': {'simpleText': 'This playlist is private.'}}}]}
    bridge = SnapshotBridge([menu, *[unavailable] * 4])
    bridge.connection_id = 1
    client = build_client(SimpleNamespace(brand_account_id='wrong' if wrong_context else 'PRIVATE_BRAND'), bridge)
    clients = {main: object(), other: client}
    undo = json.dumps({'deletions': [{'identity': other, 'ytm': 'PL_MISSING', 'phase': 'uncertain'}]})
    aid = store.actions.record_action('gc_generated', '{}', '{}', 'delete_pending', undo, 1)
    before = store.actions.get_action(aid)
    response = _client(store, lambda: clients).get(f'/actions/{aid}/deletions/0/snapshot-report')

    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['account_id'] == other
    assert 'PRIVATE_' not in response.text and 'PL_MISSING' not in response.text
    reads = response.json()['reads']
    if wrong_context:
        assert reads == {'scope': {'request_account': 'delegated',
                                   'configured_account_matches': False, 'account_menu': 'not_checked'}}
        assert bridge.calls == []
    else:
        assert reads['scope'] == {'request_account': 'delegated', 'configured_account_matches': True,
                                  'account_menu': 'authenticated', 'bridge_connection_unchanged': True}
        assert reads['ordinary']['failed_page']['envelope']['alert_sample'] == [
            {'renderer': 'alertRenderer', 'type': 'ERROR', 'message_class': 'reported_private'}]
        assert reads['youtube']['layout_probe']['header_id_matches'] is False
        assert reads['production']['exhausted'] is False
        endpoints = [urlsplit(url).path.removeprefix('/youtubei/v1/') for url, _ in bridge.calls]
        assert endpoints == ['account/account_menu', *['browse'] * 4]
        assert [body.get('browseId') for _, body in bridge.calls] == [None, *['VLPL_MISSING'] * 4]
        assert all(body['context']['user']['onBehalfOfUser'] == 'PRIVATE_BRAND' for _, body in bridge.calls)
    assert store.actions.get_action(aid) == before
    assert store.playlists.get_playlists() == []


@pytest.mark.parametrize('entry,connected,expected', [(-1, True, 404), (1, True, 404), (0, False, 409)])
def test_saved_deletion_report_rejects_invalid_entry_or_disconnected_owner(store, entry, connected, expected):
    main = store.accounts.upsert_account('Main', 'main', None, True)
    other = store.accounts.upsert_account('Other', 'other', 'brand', False)
    undo = json.dumps({'deletions': [{'identity': other, 'ytm': 'PL_MISSING'}]})
    aid = store.actions.record_action('gc_generated', '{}', '{}', 'delete_pending', undo, 1)
    clients = {main: object(), **({other: object()} if connected else {})}

    assert _client(store, lambda: clients).get(
        f'/actions/{aid}/deletions/{entry}/snapshot-report').status_code == expected
