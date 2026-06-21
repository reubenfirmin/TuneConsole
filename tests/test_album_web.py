"""Album page: renders the track table, and creates a playlist from the album."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _album(browse="MPREb_x"):
    return {browse: {
        "title": "The Album", "year": "2021",
        "artists": [{"name": "Artist X"}],
        "thumbnails": [{"url": "http://t/1.jpg", "width": 300, "height": 300}],
        "tracks": [{"title": "One", "videoId": "v1", "duration": "3:01", "artists": [{"name": "Artist X"}]},
                   {"title": "Two", "videoId": "v2", "duration": "2:40", "artists": [{"name": "Artist X"}]}],
    }}


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(albums=_album())
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), fc, iid


def test_album_page_renders_table_and_create_form(store):
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "The Album" in r.text and "Artist X" in r.text
    assert "One" in r.text and "Two" in r.text                 # the track table
    assert "/album/create-playlist" in r.text                  # the create-playlist form


def test_create_playlist_from_album_redirects_to_new_playlist(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    c, fc, iid = _client(store)
    r = c.post("/album/create-playlist", data={"browse_id": "MPREb_x", "name": "My Album Mix"})
    assert r.status_code == 200
    new_pl = next(p for p in store.get_playlists() if p.title == "My Album Mix")
    assert r.headers["hx-redirect"] == f"/playlist/{new_pl.id}"
    assert store.get_playlist_track_ids(new_pl.id)             # tracks were added
    assert fc.created and fc.added[0][1] == ["v1", "v2"]       # created on YouTube with the album's tracks


def test_create_playlist_from_album_requires_browse(store):
    c, _fc, _iid = _client(store)
    r = c.post("/album/create-playlist", data={"browse_id": "", "name": "x"})
    assert r.status_code == 422 and "album" in r.text.lower()
