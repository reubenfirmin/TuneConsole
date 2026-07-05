import json
import threading
import time
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


def test_state_pevent_updates_now_playing_paused_flag():
    # #97 a "state" pevent (posted on the video element's pause/play) flips now_playing["paused"]
    # without waiting for a fresh "play" frame.
    bridge = Bridge()
    client = _app(bridge)
    with TestClient(client).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is False

        ws.send_json({"type": "pevent", "kind": "state", "state": "paused", "videoId": "v1",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing.get("paused") is not True:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is True

        ws.send_json({"type": "pevent", "kind": "state", "state": "playing", "videoId": "v1",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing.get("paused") is not False:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is False

        # Defense-in-depth (review hardening): a state event stale-tagged with the PREVIOUS track's
        # videoId during a transition must not flip the current track's paused state.
        ws.send_json({"type": "pevent", "kind": "state", "state": "paused", "videoId": "vOLD",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        ws.send_json({"type": "pevent", "kind": "state", "state": "paused", "videoId": "v1",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing.get("paused") is not True:
            time.sleep(0.05)
        # The matching (v1) frame applied; had the vOLD frame applied it would have been
        # indistinguishable here, so assert on ordering: the vOLD frame alone must NOT have
        # flipped it before the v1 frame arrived. Re-check with only a mismatched frame:
        ws.send_json({"type": "pevent", "kind": "state", "state": "playing", "videoId": "vOLD",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        time.sleep(0.3)                     # give the socket loop time to (not) apply it
        assert bridge.now_playing["paused"] is True   # mismatched frame ignored, still paused


def test_bye_pevent_clears_now_playing():
    # #97 the YTM tab going away means nothing is playing anymore, same as a bridge disconnect.
    bridge = Bridge()
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing is not None

        ws.send_json({"type": "pevent", "kind": "bye", "videoId": "v1",
                      "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is not None:
            time.sleep(0.05)
        assert bridge.now_playing is None


def test_ws_disconnect_resets_radio_session_and_clears_setting():
    # #93 defect 1: the WS dropping is the real "tab gone" signal (unlike a pagehide "bye", which
    # also fires on the radio's own hard navigation and must NOT reset the session, see
    # tests/test_radio_react.py). Disconnecting the socket must reset the radio and flip the
    # persisted radio_active setting off.
    from yt_playlist.rec.radio import RadioSession

    bridge = Bridge()
    store = _FakeStore()
    radio = RadioSession()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store, "radio": radio})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with radio.lock:
        radio.active = True
        radio.dispatched_keys.add("k")
    store.set_setting("radio_active", "1")
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        assert radio.active is True   # still active while the socket is up
    # Socket closed on exiting the `with` block; the route's `finally` block has run.
    assert radio.active is False
    assert radio.dispatched_keys == set()
    assert store.get_setting("radio_active") == "0"


def test_play_frame_includes_paused_false():
    bridge = Bridge()
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is False


def test_play_frame_carries_true_paused_state():
    # #97 a play frame taken while the tab is actually paused (e.g. right after a server restart)
    # must report paused: true, not the old hardcoded False.
    bridge = Bridge()
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1", "paused": True})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is True


def test_now_playing_toggle_sends_playpause_control_frame():
    # Mirrors the /now-playing/rate route: POST and WS must share the same app/bridge instance.
    bridge = Bridge()
    app = _app(bridge)
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        r = client.post("/now-playing/toggle")
        assert r.json() == {"ok": True}
        frame = ws.receive_json()
        assert frame == {"type": "playpause"}


def test_now_playing_toggle_returns_ok_false_when_not_connected():
    bridge = Bridge()
    client = TestClient(_app(bridge))
    r = client.post("/now-playing/toggle")
    assert r.json()["ok"] is False


def test_pevent_frame_persists_raw_event():
    # #91 a pevent frame lands in player_events; a stub store still cannot kill the socket
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
        ws.send_json({"type": "pevent", "kind": "track_exit", "videoId": "v1",
                      "position": 20.0, "duration": 400.0, "playlist": "PL1", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and not s.player_events_since(0):
            time.sleep(0.05)
    evs = s.player_events_since(0)
    assert len(evs) == 1 and evs[0]["kind"] == "track_exit" and evs[0]["at"] == 1234.0


def test_standby_play_frame_is_ignored(monkeypatch):
    # T7f: dual-deck mode runs the content script in BOTH tabs, so the standby deck's play frame
    # must never register a play, persist, or advance the radio, only the live deck's frames count.
    from yt_playlist.core.store import Store
    from yt_playlist.rec import radio as radio_mod
    from yt_playlist.rec.radio import RadioSession
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    bridge = Bridge()
    radio = RadioSession()
    with radio.lock:
        radio.active = True
    on_play_calls = []
    # A stub that raises would be indistinguishable from "never called": the play branch already
    # wraps radio_mod.on_play in a bare except, so an unreached raise and a swallowed raise look
    # identical from here. Count calls instead so "not called" is the only way to pass.
    monkeypatch.setattr(radio_mod, "on_play", lambda *a, **k: on_play_calls.append(1))
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": s, "radio": radio,
                         "now_fn": staticmethod(lambda: 1234.0)})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "deck": "standby", "title": "Song", "artist": "Artist",
                      "thumbnail": "", "likeStatus": "INDIFFERENT", "videoId": "v1",
                      "playlist": "PLabc", "brandId": ""})
        time.sleep(0.3)   # give the socket loop a chance to (not) act on it; no event to poll for
        # Assert while the socket is still open: on disconnect the route's `finally` block resets
        # bridge.now_playing on its own, which would mask what we're testing here.
        assert bridge.now_playing is None
        assert on_play_calls == []
    assert s.play_events_since(0) == []


def test_live_play_frame_is_processed():
    # Regression: the same frame tagged deck:"live" must still set now_playing and persist, exactly
    # as an untagged frame did before deck attribution existed (see test_play_frame_persists_event).
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
        ws.send_json({"type": "play", "deck": "live", "title": "Song", "artist": "Artist",
                      "thumbnail": "", "likeStatus": "INDIFFERENT", "videoId": "v1",
                      "playlist": "PLabc", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and not s.play_events_since(0):
            time.sleep(0.05)
        # Assert while the socket is still open: on disconnect the route's `finally` block resets
        # bridge.now_playing to None regardless, which would mask what we're testing here.
        assert bridge.now_playing is not None and bridge.now_playing["video_id"] == "v1"
    evs = s.play_events_since(0)
    assert len(evs) == 1 and evs[0]["video_id"] == "v1" and evs[0]["playlist_ytm_id"] == "PLabc"


def test_standby_pevent_frame_is_ignored(monkeypatch):
    # T7f: a standby deck's pevent (fired by loading or pausing the background tab) must not flip
    # now_playing, persist, or reach the radio reactor.
    from yt_playlist.rec import radio as radio_mod
    from yt_playlist.rec.radio import RadioSession
    from yt_playlist.library import player_events
    bridge = Bridge()
    radio = RadioSession()   # active stays False until right before the standby pevent: the setup
                             # play frame above is untagged/irrelevant to this test and must not
                             # exercise the real (unmocked) radio_mod.on_play against a fake store.
    react_calls = []
    persist_calls = []
    # Call-counting stubs, not raising ones: the pevent branch already wraps both persistence and
    # radio_mod.react in bare excepts, so a raise-based "was it called" spy can't tell "called and
    # swallowed" from "never called" (the lesson from earlier raise-guard defeats).
    monkeypatch.setattr(radio_mod, "react", lambda *a, **k: react_calls.append(1))
    monkeypatch.setattr(player_events, "handle_player_event",
                        lambda *a, **k: persist_calls.append(1))
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": _FakeStore(), "radio": radio,
                         "now_fn": staticmethod(lambda: 1234.0)})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is False

        with radio.lock:
            radio.active = True
        ws.send_json({"type": "pevent", "deck": "standby", "kind": "state", "state": "paused",
                      "videoId": "v1", "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        time.sleep(0.3)   # give the socket loop a chance to (not) act on it; no event to poll for
        # Assert while the socket is still open: on disconnect the route's `finally` block resets
        # bridge.now_playing and the radio session, which would mask what we're testing here.
        assert bridge.now_playing["paused"] is False   # standby pevent must not flip the live bar
        assert react_calls == []
        assert persist_calls == []


def test_live_and_unknown_deck_pevent_still_processed():
    # Regression + interface: "live" and "unknown" tags (not just an absent tag) must still flip
    # now_playing, matching untagged frames (see test_state_pevent_updates_now_playing_paused_flag).
    bridge = Bridge()
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "deck": "live", "title": "Song", "artist": "Artist",
                      "thumbnail": "", "likeStatus": "INDIFFERENT", "videoId": "v1"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is False

        ws.send_json({"type": "pevent", "deck": "unknown", "kind": "state", "state": "paused",
                      "videoId": "v1", "position": 1.0, "duration": 2.0, "playlist": "", "brandId": ""})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing.get("paused") is not True:
            time.sleep(0.05)
        assert bridge.now_playing["paused"] is True
