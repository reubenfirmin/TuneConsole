"""YouTube authentication must use the browser bridge without a separate OAuth flow."""
import pytest
from starlette.testclient import TestClient

from yt_playlist.web.app import create_app


@pytest.mark.parametrize("method,path", [
    ("GET", "/setup/playlist-dates"),
    ("POST", "/setup/playlist-dates/connect"),
    ("GET", "/setup/playlist-dates/callback?state=old&code=unused"),
    ("POST", "/setup/playlist-dates/refresh"),
    ("POST", "/setup/playlist-dates/disconnect"),
])
def test_separate_google_connection_routes_are_unavailable(store, method, path):
    client = TestClient(create_app(store, lambda: {}), base_url="http://127.0.0.1")
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_setup_and_privacy_do_not_offer_a_separate_google_connection(store):
    client = TestClient(create_app(store, lambda: {}), base_url="http://127.0.0.1")
    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "Playlist dates" not in setup.text
    assert "/setup/playlist-dates" not in setup.text
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert "does not store Google" in privacy.text
    assert "session cookies or OAuth tokens" in privacy.text
    assert "If you connect Playlist dates" not in privacy.text
