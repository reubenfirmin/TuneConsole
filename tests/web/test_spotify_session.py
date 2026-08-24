from yt_playlist.web.spotify_session import import_client


def test_refreshes_expiring_token_and_rotates_refresh_token(store, monkeypatch):
    store.set_setting("spotify_client_id", "cid")
    store.set_setting("spotify_refresh_token", "old-refresh")
    store.set_setting("spotify_access_token", "old-access")
    store.set_setting("spotify_access_expires_at", "1010")
    monkeypatch.setattr("yt_playlist.web.spotify_session.spotify.refresh_access_token",
                        lambda *a: {"access_token": "new-access", "refresh_token": "new-refresh",
                                   "expires_in": 3600})
    client = import_client(store, 1000, session=object())
    assert client.access_token == "new-access"
    assert store.get_setting("spotify_refresh_token") == "new-refresh"
    assert store.get_setting("spotify_access_expires_at") == "4600"
