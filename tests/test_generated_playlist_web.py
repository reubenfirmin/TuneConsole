"""/playlist/saved/{account_id}/{ytm}: redirect-to-local, live-fallback and no-client paths."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


@pytest.fixture(autouse=True)
def generated_playlist_routes(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web import generated_playlist
    from yt_playlist.web.routes import audio_preview

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (audio_preview, generated_playlist)])


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def test_redirects_to_the_local_row_when_sync_has_already_imported_it(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    local = store.playlists.upsert_playlist(iid, "PLX", "Imported", 1, "h", 1.0)
    c = _client(store, lambda: {})
    r = c.get(f"/playlist/saved/{iid}/PLX", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/playlist/{local}"


def test_falls_back_to_a_live_fetch_when_no_receipt_is_cached(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = FakeClient(tracks={"PLY": [{"videoId": "v1", "title": "Song", "artists": [{"name": "Artist"}]}]})
    c = _client(store, lambda: {iid: fc})
    r = c.get(f"/playlist/saved/{iid}/PLY", follow_redirects=False)
    assert r.status_code == 200
    assert "Song" in r.text


def test_returns_503_when_no_client_is_connected_and_nothing_is_cached(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    c = _client(store, lambda: {})
    r = c.get(f"/playlist/saved/{iid}/PLZ", follow_redirects=False)
    assert r.status_code == 503
