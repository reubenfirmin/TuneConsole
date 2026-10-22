"""Contract tests for the htmx Save/Unsave album actions (/collection/save-album|unsave-album).

The routes now do their YouTube/store work and return an empty 200 with HX-Refresh: true
(htmx reloads, keeping both album tables + the saved column in sync, parity with the old
location.reload()), and surface failures via the standard 422 OOB error toast, instead of
the old JSON payloads.
"""
from fastapi.testclient import TestClient
import pytest

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


def test_artists_page_includes_saved_albums_without_playlists(store):
    store.collection.add_saved_album({"browse": "B1", "title": "The Album", "artist": "Album artist"})
    store.tracks.upsert_track("v1", "Album song", "Album artist", "The Album", 200, album_browse_id="B1")
    client = _client(store, lambda: {})

    response = client.get("/artists")
    assert response.status_code == 200
    assert "across your playlists and saved albums" in response.text
    artist, = response.context["artists"]
    assert (artist["artist"], artist["songs"], artist["n_albums"], artist["n_pls"]) == (
        "Album artist", 1, 1, 0)
    assert '/artist?name=Album%20artist' in response.text


def test_save_album_likes_and_refreshes(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/save-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r) and r.text == ""
    assert any(a["browse"] == "MPREb_x" for a in store.collection.get_saved_albums())   # mirrored locally
    assert fc.rated == [("PLAUDIO", "LIKE")]                                  # liked on YouTube


def test_unsave_album_unlikes_and_refreshes(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.collection.add_saved_album({"browse": "MPREb_x", "title": "The Album", "artist": "Artist X",
                           "year": "2001", "thumbnail": ""},identity_id=iid)
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/unsave-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r)
    assert all(a["browse"] != "MPREb_x" for a in store.collection.get_saved_albums())
    assert fc.rated == [("PLAUDIO", "INDIFFERENT")]


def test_unsave_targets_unique_non_main_owner_and_carries_owner_in_both_views(store):
    main = store.identities.upsert_identity("Main", "cred", None, True)
    other = store.identities.upsert_identity("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "B", "title": "Album", "artist": "Artist X"}, identity_id=other)
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    for path in ("/albums", "/artist?name=Artist%20X"):
        html = c.get(path).text
        assert f'name="identity_id" value="{other}"' in html
        assert "Other" in html
    response = c.post("/collection/unsave-album", data={"browse_id": "B"})
    assert _refreshes(response)
    assert clients[main].rated == []
    assert clients[other].rated == [("PLAUDIO", "INDIFFERENT")]
    assert store.collection.get_saved_albums(identity_id=other) == []


@pytest.mark.parametrize("selected", ["main", "other"])
def test_multiple_owners_require_choice_and_remove_only_selected_membership(store, selected):
    main = store.identities.upsert_identity("Main", "cred", None, True)
    other = store.identities.upsert_identity("Other", "cred", "other", False)
    for owner in (main, other):
        store.collection.add_saved_album({"browse": "B", "title": "Album", "artist": "Artist X"}, identity_id=owner)
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    html = c.get("/albums").text
    assert "Unsave from Main" in html and "Unsave from Other" in html
    assert c.post("/collection/unsave-album", data={"browse_id": "B"}).status_code == 422
    assert all(client.rated == [] for client in clients.values())
    chosen, retained = (main, other) if selected == "main" else (other, main)
    response = c.post("/collection/unsave-album", data={"browse_id": "B", "identity_id": chosen})
    assert _refreshes(response)
    assert clients[chosen].rated == [("PLAUDIO", "INDIFFERENT")]
    assert clients[retained].rated == []
    assert store.collection.get_saved_albums(identity_id=chosen) == []
    assert [a['browse'] for a in store.collection.get_saved_albums(identity_id=retained)] == ['B']


@pytest.mark.parametrize("owner_state", ["unavailable", "inactive"])
def test_unavailable_album_owner_never_falls_back_to_main(store, owner_state):
    main = store.identities.upsert_identity("Main", "cred", None, True)
    other = store.identities.upsert_identity("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "B", "title": "Album"}, identity_id=other)
    clients = {main: AlbumClient()}
    if owner_state == "inactive":
        with store.conn:
            store.conn.execute('UPDATE identities SET active=0 WHERE id=?', (other,))
        clients[other] = AlbumClient()
    c = _client(store, lambda: clients)
    response = c.post("/collection/unsave-album", data={"browse_id": "B", "identity_id": other})
    assert response.status_code == 422 and "not connected" in response.text
    assert all(client.rated == [] for client in clients.values())
    assert [a['browse'] for a in store.collection.get_saved_albums(identity_id=other)] == ['B']
    assert store.collection.saved_album_owners('B')['B'][0]['identity_id'] == other


def test_album_unsave_rejects_non_owner_and_unknown_legacy_ownership(store):
    main = store.identities.upsert_identity("Main", "cred", None, True)
    other = store.identities.upsert_identity("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "owned", "title": "Owned"}, identity_id=other)
    store.collection.add_saved_album({"browse": "legacy", "title": "Legacy"})
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    for data in ({"browse_id": "owned", "identity_id": main}, {"browse_id": "legacy"},
                 {"browse_id": "legacy", "identity_id": main}, {"browse_id": "owned", "identity_id": "bad"}):
        assert c.post("/collection/unsave-album", data=data).status_code == 422
    assert all(client.rated == [] for client in clients.values())
    assert store.collection.saved_album_ids() == {'owned', 'legacy'}


def test_save_album_without_id_returns_toast(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    c = _client(store, lambda: {iid: AlbumClient()})
    r = c.post("/collection/save-album", data={})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "no album id" in r.text
    assert store.collection.get_saved_albums() == []


def test_save_album_client_error_returns_toast(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = AlbumClient(album={})                       # no audioPlaylistId -> ValueError path
    c = _client(store, lambda: {iid: fc})
    r = c.post("/collection/save-album", data={"browse_id": "X"})
    assert r.status_code == 422
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.collection.get_saved_albums() == []           # nothing saved on failure
    assert fc.rated == []
