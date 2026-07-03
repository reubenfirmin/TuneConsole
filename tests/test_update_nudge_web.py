from starlette.testclient import TestClient
from fastapi import FastAPI


class FakeStore:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)
    def set_setting(self, key, value):
        self.settings[key] = value or ""


def _home_app(store):
    # Build only the home router with a minimal ctx. Match the ctx attributes home.build reads;
    # if home.build needs more of ctx than store/now_fn, extend this stub to supply them.
    from yt_playlist.web.routes import home
    app = FastAPI()
    # home.build destructures ctx.templates at build time (store, now_fn, templates), even though
    # the dismiss route under test never touches it, so it must be present or build() raises.
    ctx = type("C", (), {"store": store, "now_fn": lambda: 1000.0, "templates": None})()
    app.include_router(home.build(ctx))
    return app


def test_dismiss_update_records_current_latest():
    store = FakeStore({"latest_version_seen": "0.2.0"})
    client = TestClient(_home_app(store))
    r = client.post("/onboard/update/dismiss")
    assert r.status_code == 200
    assert store.get_setting("backend_update_dismissed_version") == "0.2.0"


def test_dismiss_update_records_version_param_over_latest():
    store = FakeStore({"latest_version_seen": "0.2.0"})
    client = TestClient(_home_app(store))
    r = client.post("/onboard/update/dismiss?v=0.3.0")
    assert r.status_code == 200
    assert store.get_setting("backend_update_dismissed_version") == "0.3.0"


def test_home_renders_backend_update_card(store):
    from yt_playlist.web.app import create_app
    from tests.conftest import FakeClient

    store.set_setting("last_sync_at", "1700000000")
    store.set_setting("latest_version_seen", "999.0.0")   # force "behind" regardless of real version
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    html = c.get("/").text
    assert 'id="update-nudge"' in html
    assert "999.0.0" in html
    assert "/onboard/update/dismiss" in html
