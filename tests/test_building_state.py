from fastapi.testclient import TestClient

from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)


def test_discover_building_polls_when_worker_busy(store):
    app = _app(store)
    app.state.ctx.rec_worker._running = True          # simulate an in-flight rebuild
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/home/discover").text
    assert 'hx-trigger="load delay:4s"' in html        # self-poll while building
    assert "skeleton" in html


def test_discover_serves_cached_with_no_poll_when_idle(store):
    app = _app(store)
    store.upsert_discovered_album("B", "X", "LP", "2024", None, now=1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/home/discover").text
    assert "LP" in html
    assert "load delay" not in html                    # built + idle -> no polling
    assert "refreshing" not in html
