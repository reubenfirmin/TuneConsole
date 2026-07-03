import json
import threading
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from fastapi import FastAPI
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN


class _FakeStore:
    """Minimal store stub: just enough get_setting/set_setting for the WS route's pairing flag."""
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def _app(bridge):
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": _FakeStore()})()
    app.include_router(build_bridge_route(ctx))
    return app


def test_our_extension_origin_connects_and_round_trips():
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        # A worker thread calls execute(); the WS side (this test) plays the extension.
        out = {}

        def call():
            out["res"] = bridge.execute("POST", "https://music.youtube.com/youtubei/v1/browse", {"b": 1}, timeout=5)

        t = threading.Thread(target=call)
        t.start()
        frame = ws.receive_json()
        assert frame["body"] == {"b": 1}
        ws.send_json({"id": frame["id"], "status": 200, "body": json.dumps({"ok": True})})
        t.join(timeout=5)
        assert out["res"] == (200, json.dumps({"ok": True}))


def test_wrong_origin_rejected():
    # A web page cannot forge the Origin header, so a non-extension origin must be refused. This is
    # what keeps a random localhost-probing site from driving the bridge.
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/bridge/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_dev_extension_origin_accepted():
    # An unpacked load of extension/ derives a different id than the store build (manifest `key`
    # vs the store's signing key); both are first-party, so both origins must connect.
    from yt_playlist.web.routes.bridge import EXTENSION_ORIGINS
    dev = next(o for o in EXTENSION_ORIGINS if o != EXTENSION_ORIGIN)
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": dev}):
        assert bridge.connected


def test_missing_origin_rejected():
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/bridge/ws") as ws:
            ws.receive_json()


def test_play_frame_persists_event():
    # #75 a play frame updates now_playing AND lands in play_events with identity attribution
    import time
    from yt_playlist.core.store import Store
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": s,
                         "now_fn": staticmethod(lambda: 1234.0)})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1",
                      "playlist": "PLabc", "brandId": ""})
        deadline = time.time() + 5           # the route persists via a worker thread; poll for it
        while time.time() < deadline and not s.play_events_since(0):
            time.sleep(0.05)
    evs = s.play_events_since(0)
    assert len(evs) == 1
    assert evs[0]["video_id"] == "v1" and evs[0]["playlist_ytm_id"] == "PLabc"
