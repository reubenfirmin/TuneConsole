"""Contract tests for the htmx Save/Unsave album actions (/collection/save-album|unsave-album).

The routes now do their YouTube/store work and return an empty 200 with HX-Refresh: true
(htmx reloads, keeping both album tables + the saved column in sync — parity with the old
location.reload()), and surface failures via the standard 422 OOB error toast, instead of
the old JSON payloads.
"""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


class AlbumClient(FakeClient):
    """FakeClient that can resolve + rate an album. Pass album={} to simulate a bad album."""
    def __init__(self, album=None, **kw):
        super().__init__(**kw)
        self.rated = []
        self._album = {"audioPlaylistId": "PLAUDIO", "title": "The Album", "type": "Album",
                       "artists": [{"name": "Artist X"}], "year": "2001", "thumbnails": []} \
            if album is None else album

    def get_album(self, browse_id):
        return self._album

    def rate_playlist(self, playlist_id, rating):
        self.rated.append((playlist_id, rating))


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _refreshes(r):
    return r.status_code == 200 and r.headers.get("hx-refresh") == "true"


def test_save_album_likes_and_refreshes(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/save-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r) and r.text == ""
    assert any(a["browse"] == "MPREb_x" for a in store.get_saved_albums())   # mirrored locally
    assert fc.rated == [("PLAUDIO", "LIKE")]                                  # liked on YouTube


def test_unsave_album_unlikes_and_refreshes(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.add_saved_album({"browse": "MPREb_x", "title": "The Album", "artist": "Artist X",
                           "year": "2001", "thumbnail": ""})
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/unsave-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r)
    assert all(a["browse"] != "MPREb_x" for a in store.get_saved_albums())
    assert fc.rated == [("PLAUDIO", "INDIFFERENT")]


def test_save_album_without_id_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = _client(store, lambda: {iid: AlbumClient()})
    r = c.post("/collection/save-album", data={})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "no album id" in r.text
    assert store.get_saved_albums() == []


def test_save_album_client_error_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = AlbumClient(album={})                       # no audioPlaylistId -> ValueError path
    c = _client(store, lambda: {iid: fc})
    r = c.post("/collection/save-album", data={"browse_id": "X"})
    assert r.status_code == 422
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_saved_albums() == []           # nothing saved on failure
    assert fc.rated == []
