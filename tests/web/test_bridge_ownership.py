"""Exercise overlapping lifetimes through the WebSocket route, on one event loop."""
import asyncio
import threading
from types import SimpleNamespace

from anyio import CancelScope
import pytest
from starlette.websockets import WebSocketDisconnect

from yt_playlist.core.bridge import Bridge, BridgeError
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN, build


class Socket:
    headers = {"origin": EXTENSION_ORIGIN}

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.waiting = asyncio.Event()

    async def accept(self):
        pass

    async def send_json(self, frame):
        pass

    async def receive_json(self):
        self.waiting.set()
        frame = await self.incoming.get()
        if frame is None:
            raise WebSocketDisconnect()
        return frame


def handler(bridge, suspend):
    radio = SimpleNamespace(suspend=suspend, prepare=lambda: None)
    router = build(SimpleNamespace(bridge=bridge, store=None, radio=radio))
    return next(route.endpoint for route in router.routes if route.path == "/bridge/ws")


async def connect(endpoint):
    socket = Socket()
    task = asyncio.create_task(endpoint(socket))
    await asyncio.wait_for(socket.waiting.wait(), 2)
    return socket, task


async def deliver(socket, frame):
    socket.waiting.clear()
    socket.incoming.put_nowait(frame)
    await asyncio.wait_for(socket.waiting.wait(), 2)


@pytest.mark.parametrize("stale", [
    {"type": "now-heartbeat", "title": "Old song", "videoId": "old"},
    {"type": "play", "title": "Old song", "videoId": "old"},
    {"type": "sensor-health", "healthy": False},
    {"type": "pevent", "kind": "bye"},
    {"type": "pevent", "kind": "state", "state": "paused", "videoId": "new"},
    {"id": 999, "status": 200, "body": "stale", "timeZone": "Old/Zone"},
    None,
])
def test_superseded_socket_cannot_publish_or_suspend_replacement(stale):
    async def scenario():
        bridge, suspensions = Bridge(), []
        endpoint = handler(bridge, lambda: suspensions.append(True))
        a, task_a = await connect(endpoint)
        b, task_b = await connect(endpoint)
        await deliver(b, {"type": "now-heartbeat", "title": "New song", "videoId": "new",
                          "playlist": "PLnew", "paused": False})
        await deliver(b, {"type": "sensor-health", "healthy": True})
        snapshot = (dict(bridge.now_playing), bridge.now_playing_seen_at, dict(bridge.sensor_health))
        from concurrent.futures import Future
        pending = Future()
        bridge._pending[999] = pending
        a.incoming.put_nowait(stale)
        await asyncio.wait_for(task_a, 2)
        assert bridge.connected
        assert (bridge.now_playing, bridge.now_playing_seen_at, bridge.sensor_health) == snapshot
        assert not suspensions and not pending.done() and bridge.source_timezone is None
        b.incoming.put_nowait(None)
        await asyncio.wait_for(task_b, 2)
        assert not bridge.connected
        assert bridge.now_playing is bridge.now_playing_seen_at is bridge.sensor_health is None
        assert len(suspensions) == 1
        with pytest.raises(BridgeError, match="disconnected"):
            pending.result()

    asyncio.run(scenario())


def test_replacement_waits_for_current_socket_radio_suspension():
    async def scenario():
        bridge = Bridge()
        started, release = threading.Event(), threading.Event()

        def suspend():
            started.set()
            assert release.wait(2)

        endpoint = handler(bridge, suspend)
        a, task_a = await connect(endpoint)
        a.incoming.put_nowait(None)
        try:
            assert await asyncio.to_thread(started.wait, 2)
            b = Socket()
            task_b = asyncio.create_task(endpoint(b))
            await asyncio.sleep(0)
            assert not bridge.connected and not b.waiting.is_set()
        finally:
            release.set()
        await asyncio.wait_for(task_a, 2)
        await asyncio.wait_for(b.waiting.wait(), 2)
        assert bridge.connected
        await deliver(b, {"type": "now-heartbeat", "title": "New song", "videoId": "new"})
        assert bridge.now_playing["video_id"] == "new"
        b.incoming.put_nowait(None)
        await asyncio.wait_for(task_b, 2)

    asyncio.run(scenario())


def test_cancelled_socket_finishes_radio_suspension():
    async def scenario():
        bridge, suspensions = Bridge(), []
        endpoint = handler(bridge, lambda: suspensions.append(True))

        class CancelledSocket(Socket):
            async def receive_json(self):
                # TestClient cancels the socket's scope when its context exits.
                scope.cancel()
                raise WebSocketDisconnect()

        with CancelScope() as scope:
            await endpoint(CancelledSocket())
        assert not bridge.connected
        assert suspensions == [True]

    asyncio.run(scenario())
