"""Support reads use the selected account without requiring or modifying cached membership."""
import json

import pytest

from tests.web_support import _client
from yt_playlist.web.routes import playlists
from yt_playlist.web.routes import actions


def test_liked_music_report_without_cached_playlist_selects_explicit_account(store, monkeypatch):
    main = store.identities.upsert_identity('Main', 'main', None, True)
    other = store.identities.upsert_identity('Other', 'other', 'brand', False)
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
        'identity_id': other, 'reads': {'ordinary': {'advertised': 51, 'returned': 47}}}
    assert calls == [(clients[other], 'LM')]
    assert store.playlists.get_playlists() == []


@pytest.mark.parametrize('configured,connected', [(False, True), (True, False)])
def test_liked_music_report_never_falls_back_to_another_account(store, monkeypatch, configured, connected):
    main = store.identities.upsert_identity('Main', 'main', None, True)
    other = store.identities.upsert_identity('Other', 'other', 'brand', False) if configured else main + 1
    clients = {main: object()}
    if connected:
        clients[other] = object()

    def forbidden(*args):
        pytest.fail('An unavailable account must not issue a remote read')

    monkeypatch.setattr(playlists, 'playlist_read_report', forbidden)
    response = _client(store, lambda: clients).get(f'/liked-music/{other}/snapshot-report')

    assert response.status_code == 409
    assert store.playlists.get_playlists() == []


def test_saved_deletion_report_uses_owning_account_without_a_local_playlist_or_state_write(store, monkeypatch):
    main = store.identities.upsert_identity('Main', 'main', None, True)
    other = store.identities.upsert_identity('Other', 'other', 'brand', False)
    clients = {main: object(), other: object()}
    undo = json.dumps({'deletions': [{'identity': other, 'ytm': 'PL_MISSING', 'phase': 'uncertain'}]})
    aid = store.actions.record_action('gc_generated', '{}', '{}', 'delete_pending', undo, 1)
    before = store.actions.get_action(aid)
    calls = []

    def report(client, playlist_id):
        calls.append((client, playlist_id))
        return {'production': {'error': 'Incomplete'}}

    monkeypatch.setattr(actions, 'playlist_read_report', report)
    response = _client(store, lambda: clients).get(f'/actions/{aid}/deletions/0/snapshot-report')

    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['identity_id'] == other
    assert calls == [(clients[other], 'PL_MISSING')]
    assert store.actions.get_action(aid) == before
    assert store.playlists.get_playlists() == []


@pytest.mark.parametrize('entry,connected,expected', [(-1, True, 404), (1, True, 404), (0, False, 409)])
def test_saved_deletion_report_rejects_invalid_entry_or_disconnected_owner(store, monkeypatch, entry, connected, expected):
    main = store.identities.upsert_identity('Main', 'main', None, True)
    other = store.identities.upsert_identity('Other', 'other', 'brand', False)
    undo = json.dumps({'deletions': [{'identity': other, 'ytm': 'PL_MISSING'}]})
    aid = store.actions.record_action('gc_generated', '{}', '{}', 'delete_pending', undo, 1)
    clients = {main: object(), **({other: object()} if connected else {})}

    def forbidden(*args):
        pytest.fail('Invalid or disconnected targets must not issue remote requests')

    monkeypatch.setattr(actions, 'playlist_read_report', forbidden)
    assert _client(store, lambda: clients).get(
        f'/actions/{aid}/deletions/{entry}/snapshot-report').status_code == expected
