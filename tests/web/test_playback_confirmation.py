"""The card needs evidence of playback, including from store builds without replies."""
import asyncio
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from tests.web.test_bridge_route import _app
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.mark.parametrize("reply", [
    {"ok": True, "paused": True, "videoId": "song"},
    {"ok": False, "error": "Music control is unavailable"},
    {"ok": True, "paused": False, "videoId": "song"},
    {"ok": True, "paused": True, "videoId": "other"},
    {"ok": True},
    None,
])
def test_card_requires_matching_confirmed_state(reply):
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        bridge.now_playing = {"video_id": "song", "paused": False}
        responses = []
        call = threading.Thread(target=lambda: responses.append(client.post(
            "/now-playing/toggle?action=pause&videoId=song&confirm=true")))
        call.start()
        frame = ws.receive_json()
        assert frame == {"id": frame["id"], "type": "playpause", "action": "pause", "videoId": "song"}
        assert call.is_alive(), "local scheduling alone must not report success"
        ws.send_json({"id": frame["id"], "status": 200, "body": json.dumps(reply)})
        call.join(timeout=3)
        assert not call.is_alive()
        assert responses[0].json()["ok"] is (reply == {"ok": True, "paused": True, "videoId": "song"})
        assert bridge._pending == {}


@pytest.mark.parametrize("paused", [True, False])
@pytest.mark.parametrize("delayed_health", [False, True])
def test_store_build_without_reply_can_confirm_without_an_elapsed_clock(paused, delayed_health):
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        if not delayed_health:
            ws.send_json({"type": "sensor-health", "ytmTabs": 1})
        bridge.now_playing = {"video_id": "song", "paused": not paused, "position": None}
        bridge.now_playing_seen_at = time.monotonic()
        responses = []
        action = "pause" if paused else "play"
        call = threading.Thread(target=lambda: responses.append(client.post(
            f"/now-playing/toggle?action={action}&videoId=song&confirm=true")))
        call.start()
        frame = ws.receive_json()
        assert frame["type"] == "playpause" and frame["action"] == action
        # A legacy store worker executes but never acknowledges this frame.
        ws.send_json({"type": "now-heartbeat", "title": "Song", "videoId": "song",
                      "paused": paused, "position": None})
        if delayed_health:
            time.sleep(.15)
            assert call.is_alive(), "unknown capability cannot use the legacy heartbeat fallback"
            ws.send_json({"type": "sensor-health", "ytmTabs": 1})
        call.join(timeout=3)
        assert not call.is_alive()
        assert responses[0].json() == {"ok": True, "paused": paused}
        assert bridge._pending == {}, "cancel the obsolete acknowledgement wait without replay"


@pytest.mark.parametrize("reported", [False, True])
def test_current_or_unknown_worker_cannot_bypass_native_confirmation_with_a_heartbeat(reported):
    bridge = Bridge()
    client = TestClient(_app(bridge))
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        if reported:
            ws.send_json({"type": "sensor-health", "playbackConfirmation": True})
            deadline = time.monotonic() + 2
            while bridge.playback_confirmation is not True and time.monotonic() < deadline:
                time.sleep(.01)
            assert bridge.playback_confirmation is True
        else:
            assert bridge.playback_confirmation is None
        bridge.now_playing = {"video_id": "song", "paused": True}
        responses = []
        call = threading.Thread(target=lambda: responses.append(client.post(
            "/now-playing/toggle?action=play&videoId=song&confirm=true")))
        call.start()
        frame = ws.receive_json()
        ws.send_json({"type": "now-heartbeat", "title": "Song", "videoId": "song",
                      "paused": False, "position": 0})
        time.sleep(.15)
        assert call.is_alive(), "a current or unknown worker must confirm real playback, including readiness"
        ws.send_json({"id": frame["id"], "status": 200, "body": json.dumps(
            {"ok": False, "error": "Playback is stalled"})})
        call.join(timeout=3)
        assert not call.is_alive()
        assert responses[0].json() == {"ok": False, "error": "Playback is stalled"}
    assert bridge.playback_confirmation is None


def test_stale_state_cannot_turn_uncertain_delivery_into_success(monkeypatch):
    bridge = Bridge()
    client = TestClient(_app(bridge))

    async def uncertain(*args, **kwargs):
        await asyncio.sleep(.15)
        raise RuntimeError("lost reply")

    monkeypatch.setattr(bridge, "request_control", uncertain)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}):
        bridge.now_playing = {"video_id": "song", "paused": True}
        bridge.now_playing_seen_at = time.monotonic() - 1
        result = client.post("/now-playing/toggle?action=pause&videoId=song&confirm=true").json()
        assert result["ok"] is False


@pytest.mark.parametrize("query", [
    "action=pause&videoId=other", "action=pause", "action=toggle&videoId=song",
])
def test_confirmation_rejects_stale_or_ambiguous_controls(query):
    bridge = Bridge()
    client = TestClient(_app(bridge))
    bridge.now_playing = {"video_id": "song", "paused": False}
    assert client.post(f"/now-playing/toggle?{query}&confirm=true").json()["ok"] is False
    assert bridge._pending == {}
