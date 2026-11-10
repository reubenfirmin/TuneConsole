"""GET /bridge/status: lets the Setup page poll for a live extension connection, and confirms a
successful pairing (a valid-token WS connect) marks the bridge as paired so credentials_present
flips to True once the extension connects (the gap flagged in an earlier task)."""
from starlette.testclient import TestClient
from fastapi import FastAPI
import pytest
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN


def test_bridge_status_reports_connection(store):
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    assert client.get("/bridge/status").json() == {"connected": False, "now_playing": None,
                                                   "sensor_health": None,
                                                   "radio": False, "radio_ready": False,
                                                   "resumable_player": None, "radio_resume": None}


def test_bridge_status_true_while_extension_connected(store):
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        assert client.get("/bridge/status").json() == {"connected": True, "now_playing": None,
                                                       "sensor_health": None,
                                                       "radio": False, "radio_ready": False,
                                                       "resumable_player": None, "radio_resume": None}


@pytest.mark.parametrize('paused', [False, True], ids=['recording', 'maintenance'])
def test_extension_connect_marks_bridge_paired(store, paused):
    # The gap Task 5 flagged: credentials_present reads store.settings.get_setting("bridge_paired"), but
    # nothing ever set it. A successful pairing (our extension connecting) must set it.
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    assert store.settings.get_setting("bridge_paired") is None
    with store._lock:
        store.activity_paused = paused
        store.conn.execute('PRAGMA query_only=' + ('ON' if paused else 'OFF'))
    try:
        with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
            assert client.get('/bridge/status').json()['connected'] is True
        assert store.settings.get_setting("bridge_paired") == (None if paused else "1")
        assert store.conn.execute('PRAGMA query_only').fetchone()[0] == int(paused)
    finally:
        with store._lock:
            store.conn.execute('PRAGMA query_only=OFF')
            store.activity_paused = False
    if paused:
        with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
            pass
    assert store.settings.get_setting("bridge_paired") == "1"
