"""Exercise overlapping lifetimes through the WebSocket route, on one event loop."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from anyio import CancelScope
import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from yt_playlist.core.bridge import Bridge, BridgeError
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN, build
from tests.repository_mock_support import no_database


class Socket:
    headers = {"origin": EXTENSION_ORIGIN}

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.application_state = WebSocketState.CONNECTING

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

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


@pytest.mark.usefixtures('no_database')
def test_resource_accepts_observation_capability_without_context_or_storage(monkeypatch):
    from yt_playlist.web.context import Ctx

    def forbidden(*args, **kwargs):
        pytest.fail('An injected resource must not compose a replacement controller')

    monkeypatch.setattr(Ctx, 'bridge_observations', forbidden)
    observations = Mock(spec_set=['connection', 'prepare_radio', 'receive', 'retire'])

    async def scenario():
        bridge = Bridge()
        router = build(SimpleNamespace(bridge=bridge), observations=observations)
        endpoint = next(route.endpoint for route in router.routes if route.path == '/bridge/ws')
        socket, task = await connect(endpoint)
        observations.connection.assert_called_once_with()
        frame = {'type': 'now-heartbeat', 'title': 'Song', 'videoId': 'v1', 'playlist': 'PL1'}
        await deliver(socket, frame)
        observations.prepare_radio.assert_called_once_with(bridge.now_playing, frame)
        socket.incoming.put_nowait(None)
        await asyncio.wait_for(task, 2)
        observations.retire.assert_called_once_with()
        assert not bridge.connected

    asyncio.run(scenario())


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


def test_cancellation_while_receiving_retires_socket_and_joins_transport_tasks():
    async def scenario():
        bridge, suspensions = Bridge(), []
        endpoint = handler(bridge, lambda: suspensions.append(True))
        socket = Socket()
        baseline = asyncio.all_tasks()

        with CancelScope() as scope:
            async def cancel_when_receiving():
                await socket.waiting.wait()
                scope.cancel()

            cancellation = asyncio.create_task(cancel_when_receiving())
            await endpoint(socket)

        await cancellation
        assert not bridge.connected
        assert suspensions == [True]
        assert not (asyncio.all_tasks() - baseline)

    asyncio.run(scenario())


@pytest.mark.usefixtures('no_database')
def test_observation_waiting_for_provider_reply_does_not_block_socket_pump(monkeypatch):
    from yt_playlist.core.activity import activity_for
    from yt_playlist.library import live_plays
    from yt_playlist.web.routes import bridge as routes
    finished = threading.Event()
    results = []

    async def scenario():
        bridge = Bridge()
        ctx = SimpleNamespace(bridge=bridge, store=object(), radio=None)
        monkeypatch.setattr(routes, '_record_connection', lambda ctx: None)

        def observe(ctx, body, now):
            # This provider read requires this same socket's receive loop.
            reply = bridge.execute('GET', '/provider-read', None, timeout=1)
            results.append((body['videoId'], *reply))
            finished.set()

        monkeypatch.setattr(live_plays, 'handle_play_event', observe)
        endpoint = next(route.endpoint for route in build(ctx).routes if route.path == '/bridge/ws')

        class ReplySocket(Socket):
            async def send_json(self, frame):
                if frame.get('url') == '/provider-read':
                    self.incoming.put_nowait(dict(id=frame['id'], status=200, body='confirmed'))

        socket = ReplySocket()
        task = asyncio.create_task(endpoint(socket))
        try:
            await asyncio.wait_for(socket.waiting.wait(), 1)
            await deliver(socket, dict(type='play', videoId='exact-upload', title='Song', brandKnown=True))
            assert await asyncio.to_thread(finished.wait, 2), 'Observation blocked its own provider response'
            assert results == [('exact-upload', 200, 'confirmed')]
            assert bridge.connected
        finally:
            socket.incoming.put_nowait(None)
            await asyncio.wait_for(task, 2)
            activity_for(ctx).stop_executor()

    asyncio.run(scenario())


@pytest.mark.parametrize('replacement', [False, True])
def test_failed_send_during_pairing_retires_only_its_socket(monkeypatch, replacement):
    from yt_playlist.web.routes import bridge as routes

    entered, release = threading.Event(), threading.Event()

    def pairing(_ctx):
        entered.set()
        assert release.wait(2), 'Pairing was not released'

    monkeypatch.setattr(routes, '_record_connection', pairing)

    async def scenario():
        bridge, suspensions = Bridge(), []
        endpoint = handler(bridge, lambda: suspensions.append(True))
        incoming = asyncio.Queue()
        incoming.put_nowait({'type': 'websocket.connect'})

        async def send(message):
            if message['type'] == 'websocket.send':
                raise OSError('The extension disconnected during pairing')

        socket = WebSocket({'type': 'websocket', 'headers': [
            (b'origin', EXTENSION_ORIGIN.encode())]}, incoming.get, send)
        task = asyncio.create_task(endpoint(socket))
        next_socket = next_task = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            with pytest.raises(WebSocketDisconnect):
                await bridge._send({'ping': 1})
            assert socket.application_state == WebSocketState.DISCONNECTED
            if replacement:
                monkeypatch.setattr(routes, '_record_connection', lambda _ctx: None)
                next_socket, next_task = await connect(endpoint)
                await deliver(next_socket, {'type': 'now-heartbeat', 'title': 'New song', 'videoId': 'new'})
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
            if next_task is not None:
                try:
                    assert bridge.connected and bridge.now_playing['video_id'] == 'new'
                    assert not suspensions
                finally:
                    next_socket.incoming.put_nowait(None)
                    await asyncio.wait_for(next_task, 2)
        assert not bridge.connected
        assert suspensions == [True]

    asyncio.run(scenario())
