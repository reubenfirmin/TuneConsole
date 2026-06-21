from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def test_home_is_default_route(store):
    c = _client(store)
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="home"' in r.text          # Home shell marker
    assert "Never synced" in r.text        # sync card shows status (no last_sync_at yet)


def test_playlists_moved_to_slash_playlists(store):
    c = _client(store)
    assert c.get("/playlists").status_code == 200


def test_home_renders_for_you_and_no_sync_elsewhere(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)
    day = 86400.0
    now = 200 * day
    store.add_history_snapshot(iid, now - 120 * day, ["gem|x"])
    store.add_history_snapshot(iid, now - 110 * day, ["gem|x"])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now)
    c = TestClient(app, base_url="http://127.0.0.1")

    home = c.get("/").text
    assert "More in your wheelhouse" in home    # the exploit lane heading
    assert "Gem" in home                       # the forgotten gem is rendered
    assert 'class="sync-bar"' in home          # Sync control present on Home

    # Sync control removed from the other tabs (Rediscover is deleted in Task 8)
    assert 'class="sync-bar"' not in c.get("/playlists").text
    assert 'class="sync-bar"' not in c.get("/charts").text
