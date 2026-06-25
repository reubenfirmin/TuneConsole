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


def test_unsaved_album_shows_full_live_tracks_not_incidental_library_subset(store):
    """An album that merely shares a track with one of your playlists must still render the FULL
    live-fetched album, not the partial library subset. Regular sync stamps each track's
    album_browse_id, so an unsaved album can have a single incidental library row — that must not
    shadow the real 8-track album."""
    # one incidental library track tagged with this album's browse_id (as playlist sync would)
    store.upsert_track("v1", "One", "Artist X", "The Album", None, album_browse_id="MPREb_x")
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "One" in r.text and "Two" in r.text   # BOTH live tracks, not just the incidental one


def test_album_head_tools_show_share_and_enrich_icons(store):
    """The album head shows the share + enrich icons (parity with playlists), even when unsaved."""
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "/album/MPREb_x/share.txt" in r.text          # share icon links to the album .txt
    assert 'aria-label="Enrich"' in r.text               # the single waterfall enrich icon


def test_album_share_txt_lists_live_track_urls(store):
    c, _fc, _iid = _client(store)
    r = c.get("/album/MPREb_x/share.txt")
    assert r.status_code == 200
    assert "https://music.youtube.com/watch?v=v1" in r.text
    assert "https://music.youtube.com/watch?v=v2" in r.text
    assert "attachment" in r.headers["content-disposition"]


def test_album_share_uses_full_live_album_not_incidental_library_track(store):
    """An unsaved album with one incidental library track (stamped by playlist sync) must still share
    the FULL live album, not just that one track."""
    store.upsert_track("v1", "One", "Artist X", "The Album", None, album_browse_id="MPREb_x")
    c, _fc, _iid = _client(store)
    r = c.get("/album/MPREb_x/share.txt")
    assert r.status_code == 200
    assert "v1" in r.text and "v2" in r.text   # both album tracks, not just the incidental v1
    assert len([ln for ln in r.text.splitlines() if ln.strip()]) == 2


def test_create_playlist_from_album_requires_browse(store):
    c, _fc, _iid = _client(store)
    r = c.post("/album/create-playlist", data={"browse_id": "", "name": "x"})
    assert r.status_code == 422 and "album" in r.text.lower()


def test_saved_album_folds_in_durations_and_renders_liked_and_plays(store):
    """Saving an album folds its live tracks into the library WITH durations parsed from the YTM
    'M:SS' strings (issue: Length not populated), and the saved table renders the liked heart and
    the Plays column — parity with the playlist view."""
    store.add_saved_album({"browse": "MPREb_x", "title": "The Album", "artist": "Artist X"})
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "3:01" in r.text and "2:40" in r.text          # durations parsed and rendered back as M:SS
    assert "like-btn" in r.text                            # the liked heart toggle
    assert ">Plays<" in r.text                             # the Plays column header
    # the folded-in tracks actually carry the parsed duration (seconds), not None
    assert {t["title"]: t["duration"] for t in store.album_tracks_detail("MPREb_x")} \
        == {"One": 181, "Two": 160}
