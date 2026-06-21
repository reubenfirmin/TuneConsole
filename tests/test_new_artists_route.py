from fastapi.testclient import TestClient

from yt_playlist.rec_dao import RecDao
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_new_artists_fragment_serves_cached(store):
    iid = store.upsert_identity("main", "cred", None, True)
    RecDao(store).put_proposals("new_artists", [{"artist": "Donato Dozzy", "score": 1.0,
                                                 "because": ["Recondite", "Rrose"], "fits": ["Deep Focus"]}], now=1.0)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/home/new-artists").text
    assert "Donato Dozzy" in html
    assert "because you play Recondite, Rrose" in html
    assert "fits your Deep Focus" in html


def test_new_artists_building_polls(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    app.state.ctx.rec_worker._running = True
    c = TestClient(app, base_url="http://127.0.0.1")
    assert 'hx-trigger="load delay:4s"' in c.get("/home/new-artists").text
