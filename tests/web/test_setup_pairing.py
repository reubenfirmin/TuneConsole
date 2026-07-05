"""GET /bridge/status: lets the Setup page poll for a live extension connection, and confirms a
successful pairing (a valid-token WS connect) marks the bridge as paired so credentials_present
flips to True once the extension connects (the gap flagged in an earlier task)."""
from starlette.testclient import TestClient
from fastapi import FastAPI
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN


class _FakeStore:
    """Minimal store stub: just enough get_setting/set_setting for the WS route to record pairing."""
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_bridge_status_reports_connection():
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": _FakeStore()})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    assert client.get("/bridge/status").json() == {"connected": False, "now_playing": None,
                                                   "radio": False, "radio_waiting": False,
                                                   # #93 radio flag rides along; waiting-state net.
                                                   # Visibility wave: mode transparency + fallback
                                                   # diagnostics + "Up next" keys, always present
                                                   # (radio is absent from this bare ctx entirely).
                                                   "radio_dual": False, "radio_fallback_reason": None,
                                                   "radio_upcoming": []}


def test_bridge_status_true_while_extension_connected():
    bridge = Bridge()
    app = FastAPI()
    store = _FakeStore()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        assert client.get("/bridge/status").json() == {"connected": True, "now_playing": None,
                                                       "radio": False, "radio_waiting": False,
                                                       "radio_dual": False, "radio_fallback_reason": None,
                                                       "radio_upcoming": []}


def test_extension_connect_marks_bridge_paired():
    # The gap Task 5 flagged: credentials_present reads store.get_setting("bridge_paired"), but
    # nothing ever set it. A successful pairing (our extension connecting) must set it.
    bridge = Bridge()
    app = FastAPI()
    store = _FakeStore()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    assert store.get_setting("bridge_paired") is None
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        pass
    assert store.get_setting("bridge_paired") == "1"
