from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)


def test_discover_building_polls_when_worker_busy(store):
    app = _app(store)
    app.state.ctx.rec_worker._model_active = True     # simulate an in-flight rebuild
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/home/discover").text
    assert 'hx-trigger="intersect once delay:4s"' in html
    assert "skeleton" in html


def test_discover_serves_cached_with_no_poll_when_idle(store):
    from yt_playlist.rec import home_prepared

    app = _app(store)
    store.discovery.upsert_discovered_album("B", "X", "LP", "2024", None, now=1.0)
    assert home_prepared.prepare(store, 1.0) is not None
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/home/discover").text
    assert "LP" in html
    assert "load delay" not in html                    # built + idle -> no polling
    assert "refreshing" not in html
