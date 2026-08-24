from urllib.parse import parse_qs, urlparse

import pytest

from yt_playlist.providers.spotify import (SpotifyError, SpotifyImportClient, authorization_url,
                                            exchange_code, new_pkce)


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self.data


class Session:
    def __init__(self, gets=(), posts=()):
        self.gets, self.posts, self.calls = list(gets), list(posts), []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.posts.pop(0)


def test_pkce_authorization_url_has_minimal_read_scopes():
    verifier, challenge = new_pkce()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    query = parse_qs(urlparse(authorization_url("cid", "http://127.0.0.1:9999/cb",
                                                "state", challenge)).query)
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {"playlist-read-private", "playlist-read-collaborative"}
    assert query["state"] == ["state"]


def test_exchange_uses_pkce_without_client_secret():
    session = Session(posts=[Response({"access_token": "access", "refresh_token": "refresh"})])
    result = exchange_code(session, "cid", "http://127.0.0.1/cb", "code", "verifier")
    assert result["access_token"] == "access"
    data = session.calls[0][2]["data"]
    assert data["code_verifier"] == "verifier" and "client_secret" not in data


def test_playlist_and_item_pagination_maps_to_neutral_dtos():
    session = Session(gets=[
        Response({"items": [{"id": "p1", "name": "Mix", "items": {"total": 2},
                              "owner": {"display_name": "Me"}}], "next": "https://api.spotify.com/next"}),
        Response({"items": [{"id": "p2", "name": "More", "tracks": {"total": 1}}], "next": None}),
        Response({"items": [{"item": {"id": "s1", "type": "track", "name": "Song",
                                         "artists": [{"name": "Artist"}], "duration_ms": 201400,
                                         "album": {"name": "Album"},
                                         "external_ids": {"isrc": "ABC"}}},
                            {"item": {"type": "episode", "id": "pod"}},
                            {"item": {"type": "track", "name": "local"}}], "next": None}),
    ])
    client = SpotifyImportClient("token", session)
    assert [p.external_id for p in client.playlists()] == ["p1", "p2"]
    tracks = client.playlist_tracks("p1")
    assert len(tracks) == 1
    assert (tracks[0].title, tracks[0].artist, tracks[0].duration_s, tracks[0].isrc) == (
        "Song", "Artist", 201, "ABC")
    assert session.calls[1][1] == "https://api.spotify.com/next"


def test_quota_error_is_distinguishable_from_rate_limit():
    session = Session(gets=[Response({"error": {"message": "quota", "reason": "QUOTA_EXCEEDED"}},
                                     429, {"Retry-After": "17"})])
    with pytest.raises(SpotifyError) as caught:
        SpotifyImportClient("token", session).playlists()
    assert caught.value.status == 429
    assert caught.value.reason == "QUOTA_EXCEEDED" and caught.value.retry_after == 17
