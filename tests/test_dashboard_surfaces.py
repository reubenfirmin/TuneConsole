"""Dashboard surfaces: fresh songs as a proto-playlist and graphical new-artist cards."""
from fastapi.testclient import TestClient

from yt_playlist.rec_dao import RecDao
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                           base_url="http://127.0.0.1")


def test_fresh_renders_as_saveable_proto(store):
    _iid, c = _client(store)
    RecDao(store).put_proposals("fresh_songs", [
        {"video_id": "v1", "title": "New One", "artist": "Newcomer", "thumbnail": None}], now=1.0)
    html = c.get("/home/fresh").text
    assert 'id="gen-fresh"' in html                 # rendered as a proto-playlist card
    assert "Fresh songs -" in html                  # dated name
    assert "Save &amp; play on YouTube" in html     # same save flow as the other lanes
    assert "New One" in html


def test_new_artists_render_with_thumbnail(store):
    _iid, c = _client(store)
    store.upsert_discovered_artist("Donato Dozzy", 1.0, ["Recondite"], ["Deep Focus"],
                                  "https://img/dozzy.jpg", now=1.0)
    html = c.get("/home/new-artists").text
    assert "https://img/dozzy.jpg" in html          # graphical card uses the artist image
    assert "Donato Dozzy" in html
