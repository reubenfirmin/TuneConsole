import json
import threading
import time
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from fastapi import FastAPI
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN


class _FakeSettings:
    """Minimal store stub: just enough get_setting/set_setting for the WS route's pairing flag."""
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class _FakeStore:
    def __init__(self):
        self.settings = _FakeSettings()


def _app(bridge):
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": _FakeStore()})()
    app.include_router(build_bridge_route(ctx))
    return app


@pytest.mark.parametrize("starting", [False, True])
@pytest.mark.parametrize("version", [1, 2])
def test_status_attributes_saved_radio_before_controller_reattaches(store, starting, version):
    from types import SimpleNamespace
    from yt_playlist.web.radio_session import PlaylistRadio

    bridge = Bridge()
    bridge.now_playing = {"title": "Playing song", "artist": "Artist", "video_id": "v1",
                          "playlist_ytm_id": "PLRADIO", "paused": False}
    bridge.now_playing_seen_at = time.monotonic()
    store.settings.set_setting("radio_current_session", json.dumps({
        "version": version, "phase": "preparing" if starting else "playing",
        "playlist": "PLRADIO", "title": "Radio Session", "tracks": [{"video_id": "v1"}],
        "initial_tracks": [{"video_id": "v1"}] if starting else [],
    }))
    ctx = SimpleNamespace(bridge=bridge, store=store)
    ctx.radio = PlaylistRadio(ctx)
    assert ctx.radio.session is None and not ctx.radio.active
    app = FastAPI()
    app.include_router(build_bridge_route(ctx))
    with TestClient(app).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        report = TestClient(app).get("/bridge/status").json()
    assert report["radio"] is True
    assert report["radio_ready"] is (not starting)
    assert report["now_playing"]["source"]["kind"] == "radio"


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
        ws.send_json({"id": frame["id"], "status": 200, "body": json.dumps({"ok": True}),
                      "timeZone": "America/Costa_Rica"})
        t.join(timeout=5)
        assert out["res"] == (200, json.dumps({"ok": True}))
        assert bridge.source_timezone == "America/Costa_Rica"


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
    s.identities.upsert_identity("main", "bridge", None, True)
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
        while time.time() < deadline and not s.history.play_events_since(0):
            time.sleep(0.05)
    evs = s.history.play_events_since(0)
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


def test_now_playing_heartbeat_refreshes_presence_without_replacing_track():
    bridge = Bridge()
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1", "paused": True})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.05)
        original = bridge.now_playing
        bridge.now_playing_seen_at = 1.0

        ws.send_json({"type": "now-heartbeat", "deck": "unknown"})
        deadline = time.time() + 5
        while time.time() < deadline and bridge.now_playing_seen_at == 1.0:
            time.sleep(0.05)

        assert bridge.now_playing is original
        assert bridge.now_playing_seen_at > 1.0


def test_rich_now_playing_heartbeat_restores_expired_ui_without_recording_play():
    """A throttled YTM page may miss the eight-second presence window during an audio-device
    switch. Its next deduplicated heartbeat must rebuild Now Playing without entering the play
    persistence path."""
    from yt_playlist.core.store import Store
    store = Store(":memory:"); store.init_schema()
    store.identities.upsert_identity("main", "bridge", None, True)
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    with TestClient(app) as client, client.websocket_connect(
            "/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        assert bridge.now_playing is None
        ws.send_json({"type": "now-heartbeat", "deck": "unknown", "title": "Recovered",
                      "artist": "Artist", "thumbnail": "art", "likeStatus": "LIKE",
                      "videoId": "v-recovered", "playlist": "OLAK5uy_album",
                      "albumBrowseId": "MPRE_album", "paused": False})
        deadline = time.time() + 1
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.01)
        assert bridge.now_playing == {
            "title": "Recovered", "artist": "Artist", "thumbnail": "art",
            "likeStatus": "LIKE", "video_id": "v-recovered", "paused": False,
            "metadata_verified": None,
            "playlist_ytm_id": "OLAK5uy_album", "album_browse_id": "MPRE_album",
        }
        assert store.history.play_events_since(0) == []


@pytest.mark.parametrize("kind", ["play", "now-heartbeat"])
def test_playback_context_survives_sensor_frames_and_legacy_heartbeats(kind):
    bridge = Bridge()
    with TestClient(_app(bridge)) as client, client.websocket_connect(
            "/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": kind, "title": "Song", "videoId": "v1", "paused": True,
                      "playlist": "PL_source", "albumBrowseId": "MPRE_album"})
        deadline = time.time() + 1
        while time.time() < deadline and bridge.now_playing is None:
            time.sleep(0.01)
        assert bridge.now_playing["playlist_ytm_id"] == "PL_source"
        assert bridge.now_playing["album_browse_id"] == "MPRE_album"

        ws.send_json({"type": "now-heartbeat", "title": "Legacy refresh", "videoId": "v1", "paused": True})
        deadline = time.time() + 1
        while time.time() < deadline and bridge.now_playing["title"] != "Legacy refresh":
            time.sleep(0.01)
        assert bridge.now_playing["playlist_ytm_id"] == "PL_source"
        assert client.get("/bridge/status").json()["now_playing"]["source"] is None

        ws.send_json({"type": "now-heartbeat", "title": "New queue", "videoId": "v1", "paused": True,
                      "playlist": "", "albumBrowseId": ""})
        deadline = time.time() + 1
        while time.time() < deadline and bridge.now_playing["title"] != "New queue":
            time.sleep(0.01)
        assert client.get("/bridge/status").json()["now_playing"]["source"] is None


def test_legacy_heartbeat_for_another_video_does_not_inherit_source():
    from yt_playlist.web.routes.bridge import _now_playing_snapshot
    previous = {"video_id": "old", "playlist_ytm_id": "PL_old", "album_browse_id": "MPRE_old"}
    snapshot = _now_playing_snapshot({"videoId": "new", "title": "New"}, previous)
    assert "playlist_ytm_id" not in snapshot
    assert "album_browse_id" not in snapshot


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), (None, None),
                                           (1, None), ("true", None)])
def test_playback_verification_is_observed_without_inheriting_previous_state(value, expected):
    from yt_playlist.web.routes.bridge import _now_playing_snapshot

    previous = {"video_id": "same", "metadata_verified": True}
    snapshot = _now_playing_snapshot({"videoId": "same", "metadataVerified": value}, previous)
    assert snapshot["metadata_verified"] is expected
    assert _now_playing_snapshot({"videoId": "same"}, previous)["metadata_verified"] is None


def test_capture_diagnostics_retain_only_bounded_structural_fields():
    from yt_playlist.web.routes.bridge import _capture_diagnostics

    result = _capture_diagnostics({
        "metadataVerified": False, "accountKnown": True, "hasVideo": "true",
        "barCandidates": 0, "ratingCandidates": 10**6,
        "title": "private song", "brandId": "private account",
        "sources": [{"source": "bar.polymerController.data", "present": True,
                     "keys": ["x" * 100] * 12, "targetKeys": ["videoId"],
                     "data": {"title": "private song"}},
                    {"source": "private source", "present": True}, {"source": []}],
    })
    assert result["metadataVerified"] is False
    assert result["accountKnown"] is True
    assert result["hasVideo"] is None
    assert result["barCandidates"] == 0
    assert result["ratingCandidates"] is None
    assert result["sources"] == [{"source": "bar.polymerController.data", "present": True,
                                  "keys": ["x" * 32] * 8, "targetKeys": ["videoId"]}]
    assert "private" not in repr(result)
    assert _capture_diagnostics(None) is None


@pytest.mark.parametrize("thumbnail", [None, "", "https://example.com/live-art.jpg"])
@pytest.mark.parametrize("video_id", ["v_art", "unknown", None])
def test_bridge_status_falls_back_to_artwork_for_the_exact_video(store, thumbnail, video_id):
    cached_art = "https://example.com/cached-art.jpg"
    store.tracks.upsert_track("v_art", "Song", "Artist", "Album", 200, thumbnail=cached_art)
    bridge = Bridge()
    bridge.now_playing = {"title": "Song", "artist": "Artist", "video_id": video_id,
                          "thumbnail": thumbnail}
    app = FastAPI()
    app.include_router(build_bridge_route(type("C", (), {"bridge": bridge, "store": store})()))

    playing = TestClient(app).get("/bridge/status").json()["now_playing"]
    expected = thumbnail or (cached_art if video_id == "v_art" else thumbnail)
    assert playing["thumbnail"] == expected
    # Enrich the HTTP response without turning the fallback into a sensor observation.
    assert bridge.now_playing["thumbnail"] == thumbnail


@pytest.mark.parametrize("playlist, album, expected", [
    ("PL_source", "MPRE_other", {"kind": "playlist", "title": "Source", "url": "/playlist/1", "external": False}),
    ("OLAK5uy_album", "", {"kind": "album", "title": "Release", "url": "/album?browse=MPRE_album", "external": False}),
    ("", "MPRE_other", {"kind": "album", "title": "Other release", "url": "/album?browse=MPRE_other", "external": False}),
    ("PL_unknown", "MPRE_album", None),
    ("RDCLAK5uy_curated", "MPRE_album", None),
    ("RDAMVMv1", "MPRE_album", None),
    ("RDAMPLsource", "MPRE_album", None),
    ("RDEMautoplay", "", None),
    ("RDQMmix", "MPRE_album", None),
    ("", "", {"kind": "album", "title": "Release", "url": "/album?browse=MPRE_album", "external": False}),
])
def test_bridge_status_links_to_playing_source(store, playlist, album, expected):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PL_source", "Source", 1, "h", 0)
    store.tracks.upsert_track("v1", "Song", "Artist", "Release", 200, album_browse_id="MPRE_album")
    store.collection.add_saved_album({"browse": "MPRE_other", "title": "Other release"})
    bridge = Bridge()
    bridge.now_playing = {"title": "Song", "video_id": "v1", "playlist_ytm_id": playlist,
                          "album_browse_id": album}
    app = FastAPI()
    app.include_router(build_bridge_route(type("C", (), {"bridge": bridge, "store": store})()))
    assert TestClient(app).get("/bridge/status").json()["now_playing"]["source"] == expected


@pytest.mark.parametrize("active,playing,kind,url", [
    (True, "PL_radio", "radio", "/#radio"),
    (True, "PL_other", "playlist", "/playlist/2"),
    (False, "PL_radio", "playlist", "/playlist/1"),
])
def test_active_radio_links_to_specialist_view(store, active, playing, kind, url):
    from types import SimpleNamespace

    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PL_radio", "Radio Session", 1, "", 1)
    store.playlists.upsert_playlist(iid, "PL_other", "Another mix", 1, "", 1)
    bridge = Bridge()
    bridge.now_playing = {"title": "Song", "video_id": "v1", "playlist_ytm_id": playing}
    radio = SimpleNamespace(active=active, bootstrap=lambda: {"state": {
        "active": active, "playlist_id": "PL_radio", "title": "Radio Session"}})
    app = FastAPI()
    app.include_router(build_bridge_route(SimpleNamespace(bridge=bridge, store=store, radio=radio)))
    source = TestClient(app).get("/bridge/status").json()["now_playing"]["source"]
    assert source["kind"] == kind
    assert source["url"] == url


def test_sensor_health_is_observable_and_clears_on_disconnect():
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "sensor-health", "healthy": False, "ytmTabs": 1,
                      "respondingTabs": 0, "reinjectedTabs": 1,
                      "recovering": True, "reloadedTabs": 1,
                      "versions": [{"tabId": 1, "ok": False, "receiverRevision": 1,
                                    "mainVersion": 4, "mainRevision": 2, "mainAgeMs": 0}],
                      "error": "content sensor did not answer"})
        deadline = time.time() + 1
        while time.time() < deadline and bridge.sensor_health is None:
            time.sleep(0.01)
        assert client.get("/bridge/status").json()["sensor_health"] == {
            "healthy": False, "ytm_tabs": 1, "responding_tabs": 0,
            "reinjected_tabs": 1, "recovering": True, "reloaded_tabs": 1,
            "error": "content sensor did not answer",
            "versions": [{"tabId": 1, "ok": False, "receiverRevision": 1,
                          "mainVersion": 4, "mainRevision": 2, "mainAgeMs": 0, "capture": None}],
        }
    assert bridge.sensor_health is None


def test_native_rating_recovery_logs_terminal_binding_evidence_without_changing_api_state(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="yt_playlist.web.routes.bridge")
    bridge = Bridge()
    bridge.now_playing = {"video_id": "rated-song", "likeStatus": "LIKE"}
    evidence = {"receiverRevision": 1, "mainRevision": 3,
                "binding": {"renderer": True, "targetIds": [], "barIds": []}}
    with TestClient(_app(bridge)).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        for state in ("pending", "failed"):
            ws.send_json({"type": "rating-reconciliation", "state": state, "ratingId": "one",
                          "videoId": "rated-song", "attempts": 12, "elapsedMs": 30000,
                          "error": "No rating binding", "first": evidence, "last": evidence})
        # A subsequent request's response proves both preceding frames were consumed in order.
        out = {}
        worker = threading.Thread(target=lambda: out.update(result=bridge.execute(
            "GET", "https://music.youtube.com/youtubei/v1/browse", None, timeout=2)))
        worker.start()
        request = ws.receive_json()
        ws.send_json({"id": request["id"], "status": 200, "body": "{}"})
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert bridge.now_playing["likeStatus"] == "LIKE"
    records = [record for record in caplog.records if "native rating reconciliation" in record.message]
    assert [record.levelno for record in records] == [logging.INFO, logging.WARNING]
    assert '"mainRevision": 3' in records[-1].message
    assert '"targetIds": []' in records[-1].message


def test_ws_disconnect_stops_playlist_radio():
    bridge = Bridge()
    store = _FakeStore()
    class Radio:
        active = True

        def stop(self):
            self.active = False

    radio = Radio()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store, "radio": radio})()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        assert radio.active is True   # still active while the socket is up
    # Socket closed on exiting the `with` block; the route's `finally` block has run.
    assert radio.active is False


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


def test_repeated_paused_play_frames_only_refresh_presence(caplog):
    from yt_playlist.core.store import Store

    store = Store(":memory:"); store.init_schema()
    store.identities.upsert_identity("main", "bridge", None, True)
    bridge = Bridge()
    app = FastAPI()
    ctx = type("C", (), {"bridge": bridge, "store": store})()
    app.include_router(build_bridge_route(ctx))
    with TestClient(app).websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        for _ in range(2):
            previous_seen_at = bridge.now_playing_seen_at
            ws.send_json({"type": "play", "deck": "live", "title": "Song", "artist": "Artist",
                          "thumbnail": "", "likeStatus": "INDIFFERENT", "videoId": "v1",
                          "playlist": "PLabc", "paused": True})
            deadline = time.time() + 5
            while time.time() < deadline and bridge.now_playing_seen_at == previous_seen_at:
                time.sleep(0.01)
            assert bridge.now_playing_seen_at != previous_seen_at
            assert bridge.now_playing["video_id"] == "v1"
            assert bridge.now_playing["paused"] is True
            assert store.history.play_events_since(0) == []
        assert "Received play notification" not in caplog.text


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


@pytest.mark.parametrize("accepted", [True, False])
def test_next_waits_for_player_acknowledgement(accepted):
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        result = []
        worker = threading.Thread(target=lambda: result.append(client.post(
            "/now-playing/next", json={"videoId": "displayed"}).json()))
        worker.start()
        frame = ws.receive_json()
        assert frame == {"id": frame["id"], "type": "next", "videoId": "displayed"}
        assert worker.is_alive()
        ws.send_json({"id": frame["id"], "status": 200,
                      "body": json.dumps({"ok": accepted, "error": "Song changed"})})
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result == ([{"ok": True}] if accepted else [{"ok": False, "error": "Song changed"}])


@pytest.mark.parametrize("payload", [{}, {"videoId": ""}, {"videoId": "song"}])
def test_next_rejects_missing_song_or_disconnected_player(payload):
    client = TestClient(_app(Bridge()))
    assert client.post("/now-playing/next", json=payload).json()["ok"] is False


@pytest.mark.parametrize("like_status", ["LIKE", "DISLIKE", "INDIFFERENT"])
@pytest.mark.parametrize("player_ok", [True, False])
def test_confirmed_rating_updates_native_player_and_skips_only_dislikes(store, like_status, player_ok):
    from types import SimpleNamespace
    from tests.conftest import FakeClient
    from yt_playlist.library.ops import PlaylistOps

    main = store.identities.upsert_identity("Main", "bridge", "main-brand", True)
    remote = FakeClient()
    ops = PlaylistOps(store, lambda: {main: remote}, lambda: 1234)
    bridge = Bridge()
    app = FastAPI()
    app.include_router(build_bridge_route(SimpleNamespace(
        bridge=bridge, store=store, ops=lambda: ops, now_fn=lambda: 1234)))
    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        bridge.now_playing = {"video_id": "radio-song", "playlist_ytm_id": "PLRADIO"}
        result = []
        action = "dislike" if like_status == "DISLIKE" else "like"
        worker = threading.Thread(target=lambda: result.append(client.post("/now-playing/rate", json={
            "action": action, "videoId": "radio-song", "likeStatus": like_status}).json()))
        worker.start()
        frame = ws.receive_json()
        assert remote.rated == [("radio-song", like_status)]
        assert store.tracks.item_rating(main, "radio-song")["status"] == like_status
        assert frame == {"id": frame["id"], "type": "rate", "action": action,
                         "videoId": "radio-song", "likeStatus": like_status,
                         "brandId": "main-brand", "skip": False}
        ws.send_json({"id": frame["id"], "status": 200,
                      "body": json.dumps({"ok": player_ok, "error": "No next song"})})
        if like_status == "DISLIKE":
            skip = ws.receive_json()
            assert skip == {"id": skip["id"], "type": "next", "videoId": "radio-song"}
            ws.send_json({"id": skip["id"], "status": 200, "body": json.dumps({"ok": True})})
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result[0]["ok"] is True  # A failed player command cannot undo a confirmed rating.
        assert result[0].get("player_error") == (None if player_ok else "No next song")
        assert store.tracks.item_rating(main, "radio-song")["status"] == like_status


@pytest.mark.parametrize("outcome", ["ok", "rejected", "http_error"])
@pytest.mark.parametrize("like_status", [None, "LIKE", "INDIFFERENT", "DISLIKE"])
def test_rate_uses_library_api_for_exact_target_without_blocking_socket(store, monkeypatch, outcome, like_status):
    from types import SimpleNamespace
    from tests.conftest import FakeClient
    from yt_playlist.core.config import IdentityConfig
    from yt_playlist.core.identities import build_client
    from yt_playlist.library import ratings
    from yt_playlist.library.ops import PlaylistOps

    bridge = Bridge()
    other = store.identities.upsert_identity("Other", "bridge", "other-brand", False)
    main = store.identities.upsert_identity("Main", "bridge", "main-brand", True)
    remote_other = FakeClient()
    remote_main = build_client(IdentityConfig("Main", "bridge", "main-brand", True), bridge)
    ops = PlaylistOps(store, lambda: {other: remote_other, main: remote_main}, lambda: 1234)
    ctx = SimpleNamespace(bridge=bridge, store=store, ops=lambda: ops, now_fn=lambda: 1234)
    app = FastAPI()
    app.include_router(build_bridge_route(ctx))
    client = TestClient(app)
    monkeypatch.setattr(ratings, "with_retry", lambda fn: fn())
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        # Playback has already advanced and neither player-bar identity nor rating is available.
        # The API must still rate A, the song the user clicked, rather than the new playing song.
        bridge.now_playing = {"video_id": "B", "likeStatus": "LIKE"}
        result = []
        payload = {"action": "like", "videoId": "A"}
        if like_status is not None:
            payload["likeStatus"] = like_status
        worker = threading.Thread(target=lambda: result.append(client.post("/now-playing/rate",
            json=payload).json()))
        worker.start()
        frame = ws.receive_json()
        status = like_status or "LIKE"
        endpoint = {"LIKE": "like", "DISLIKE": "dislike", "INDIFFERENT": "removelike"}[status]
        assert frame["method"] == "POST"
        assert frame["url"].split("?")[0] == f"https://music.youtube.com/youtubei/v1/like/{endpoint}"
        assert frame["body"]["target"] == {"videoId": "A"}
        assert frame["body"]["context"]["user"]["onBehalfOfUser"] == "main-brand"
        body = ({"error": {"message": "Rejected", "code": 403}} if outcome == "http_error" else
                {"status": "STATUS_FAILED" if outcome == "rejected" else "STATUS_SUCCEEDED"})
        ws.send_json({"id": frame["id"], "status": 403 if outcome == "http_error" else 200,
                      "body": json.dumps(body)})
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result[0]["ok"] is (outcome == "ok")
        assert remote_other.rated == []
        rating = store.tracks.item_rating(main, "A")
        assert (rating["status"] if rating else None) == (status if outcome == "ok" else None)
        assert bridge.now_playing == {"video_id": "B", "likeStatus": "LIKE"}


def test_rate_requires_displayed_target():
    client = TestClient(_app(Bridge()))
    assert client.post("/now-playing/rate", json={"action": "like"}).json()["ok"] is False


def test_pevent_frame_persists_raw_event():
    # #91 a pevent frame lands in player_events; a stub store still cannot kill the socket
    import time
    from yt_playlist.core.store import Store
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("main", "bridge", None, True)
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
        while time.time() < deadline and not s.player_events.player_events_since(0):
            time.sleep(0.05)
    evs = s.player_events.player_events_since(0)
    assert len(evs) == 1 and evs[0]["kind"] == "track_exit" and evs[0]["at"] == 1234.0


def test_live_play_frame_is_processed():
    # Regression: the same frame tagged deck:"live" must still set now_playing and persist, exactly
    # as an untagged frame did before deck attribution existed (see test_play_frame_persists_event).
    from yt_playlist.core.store import Store
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("main", "bridge", None, True)
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
        while time.time() < deadline and not s.history.play_events_since(0):
            time.sleep(0.05)
        # Assert while the socket is still open: on disconnect the route's `finally` block resets
        # bridge.now_playing to None regardless, which would mask what we're testing here.
        assert bridge.now_playing is not None and bridge.now_playing["video_id"] == "v1"
    evs = s.history.play_events_since(0)
    assert len(evs) == 1 and evs[0]["video_id"] == "v1" and evs[0]["playlist_ytm_id"] == "PLabc"


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
