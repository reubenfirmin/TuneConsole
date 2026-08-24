from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app


def _client(store):
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1:8123")


def test_setup_shows_literal_loopback_callback_and_connect_form(store):
    html = _client(store).get("/setup?tab=import").text
    assert "Import playlists from Spotify" in html
    assert "http://127.0.0.1:8123/spotify/callback" in html
    assert 'name="client_id"' in html


def test_connect_redirects_to_pkce_and_keeps_verifier_only_in_memory(store):
    client = _client(store)
    response = client.post("/spotify/connect", data={"client_id": "client123"},
                           follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["client_id"] == ["client123"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8123/spotify/callback"]
    assert query["code_challenge_method"] == ["S256"]
    attempt = client.app.state.ctx.spotify_oauth[query["state"][0]]
    assert attempt["client_id"] == "client123" and attempt["verifier"]
    assert store.get_setting("spotify_client_id") is None


def test_callback_persists_tokens_only_after_state_validation(store, monkeypatch):
    client = _client(store)
    start = client.post("/spotify/connect", data={"client_id": "client123"},
                        follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    monkeypatch.setattr("yt_playlist.web.routes.setup.spotify.exchange_code",
                        lambda *a: {"access_token": "access", "refresh_token": "refresh",
                                   "expires_in": 3600})

    class FakeSpotify:
        def __init__(self, token): assert token == "access"
        def profile(self): return {"id": "account", "name": "Listener"}

    monkeypatch.setattr("yt_playlist.web.routes.setup.spotify.SpotifyImportClient", FakeSpotify)
    response = client.get(f"/spotify/callback?state={state}&code=ok", follow_redirects=False)
    assert response.status_code == 303 and "tab=import" in response.headers["location"]
    assert store.get_setting("spotify_refresh_token") == "refresh"
    assert store.get_setting("spotify_profile_name") == "Listener"
    assert state not in client.app.state.ctx.spotify_oauth


def test_invalid_state_never_exchanges_or_persists(store, monkeypatch):
    client = _client(store)
    monkeypatch.setattr("yt_playlist.web.routes.setup.spotify.exchange_code",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not exchange")))
    response = client.get("/spotify/callback?state=wrong&code=ok", follow_redirects=False)
    assert response.status_code == 303
    assert store.get_setting("spotify_refresh_token") is None


def test_disconnect_deletes_all_spotify_credentials(store):
    for key in ("spotify_client_id", "spotify_access_token", "spotify_refresh_token",
                "spotify_access_expires_at", "spotify_profile_id", "spotify_profile_name"):
        store.set_setting(key, "secret")
    client = _client(store)
    response = client.post("/spotify/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert all(store.get_setting(key) is None for key in (
        "spotify_client_id", "spotify_access_token", "spotify_refresh_token",
        "spotify_access_expires_at", "spotify_profile_id", "spotify_profile_name"))
