"""Support reads use the selected account without requiring or modifying cached membership."""
import pytest

from tests.web_support import _client
from yt_playlist.web.routes import playlists


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
