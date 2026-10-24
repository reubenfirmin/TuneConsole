"""Playback presence reattaches saved radio without depending on an open radio page."""
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests.radio_session_support import poc, play
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN, build


def _run_preparation(worker):
    args, options = worker
    assert options['name'] == 'radio-prepare'
    args[0]()


def test_playback_arriving_after_startup_restores_and_extends_the_last_saved_song(poc):
    original, remote, frames, workers, _ = poc
    original.start()
    play(original, 'v2')
    original.suspend()
    bridge = original.ctx.bridge
    bridge.connected = False
    bridge.now_playing = bridge.now_playing_seen_at = None
    restored = PlaylistRadio(original.ctx)
    restored.prepare()
    _run_preparation(workers[-1])
    assert restored.session is None and restored.offer is not None
    before_frames = list(frames)

    bridge.connected = True
    bridge.now_playing = {'video_id': 'v3', 'playlist_ytm_id': 'PLSESSION', 'paused': False}
    bridge.now_playing_seen_at = time.monotonic()
    restored.prepare()
    preparation = workers[-1]
    queued = len(workers)
    restored.prepare()
    assert len(workers) == queued  # Repeated heartbeats cannot queue duplicate preparation.
    _run_preparation(preparation)
    s = restored.session
    assert s.monitoring and restored.active
    assert len(workers) == queued + 1
    assert frames == before_frames and not remote.adds and not remote.removes

    # The restored observer catches up to the last song and saves its successor.
    restored.listening.observe(s)
    restored.advance(s)
    assert s.position == 2 and len(remote.ids) == 4
    assert len(remote.adds) == 1 and remote.ids[-1] not in {'v1', 'v2', 'v3'}
    assert frames == before_frames
    for _ in range(3):
        restored.prepare()
    assert len(workers) == queued + 1


def test_reconnect_restarts_one_suspended_monitor_but_never_restarts_explicit_stop(poc):
    controller, remote, frames, workers, _ = poc
    controller.start()
    play(controller, 'v2')
    controller.seeds()
    s = controller.session
    controller.suspend()
    controller._monitor(s)  # A suspended worker exits before its next observation.
    assert not s.monitoring
    controller.prepare()
    _run_preparation(workers[-1])
    assert s.monitoring and not s.stop.is_set()
    queued = len(workers)
    controller.prepare()
    assert len(workers) == queued

    controller.stop()
    controller._monitor(s)
    before = (list(remote.adds), list(remote.removes), list(frames), len(workers))
    controller.prepare()
    assert (remote.adds, remote.removes, frames, len(workers)) == before
    restarted = PlaylistRadio(controller.ctx)
    restarted.prepare()
    _run_preparation(workers[-1])
    assert restarted.session is None
    assert (remote.adds, remote.removes, frames) == before[:3]
    assert workers[-1][1]['name'] == 'radio-prepare'


def test_preparation_does_not_wait_on_a_busy_session_lock(poc):
    controller, *_ = poc
    held, release, returned = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with controller.lock:
            held.set()
            release.wait(2)

    owner = threading.Thread(target=hold)
    caller = threading.Thread(target=lambda: (controller.prepare(), returned.set()))
    owner.start()
    try:
        assert held.wait(1)
        caller.start()
        assert returned.wait(1)
    finally:
        release.set()
        owner.join(timeout=2)
        if caller.ident is not None:
            caller.join(timeout=2)


def _presence_app(bridge, prepare):
    radio = SimpleNamespace(prepare=prepare, suspend=lambda: None)
    app = FastAPI()
    app.include_router(build(SimpleNamespace(bridge=bridge, store=None, radio=radio)))
    return app


def _drain_presence(ws, bridge):
    processed = threading.Event()
    bridge.resolve = lambda *args, **kwargs: processed.set()
    # A following transport reply must still be consumed by the same response pump.
    ws.send_json({'id': 1, 'status': 200, 'body': '{}'})
    assert processed.wait(1)


@pytest.mark.parametrize('kind', ['play', 'now-heartbeat'])
def test_fresh_presence_schedules_reattachment_without_requesting_radio_status(kind):
    bridge, calls = Bridge(), []
    app = _presence_app(bridge, lambda: calls.append(dict(bridge.now_playing)))
    with TestClient(app).websocket_connect('/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as ws:
        assert not calls  # Connecting alone does not establish playback ownership.
        ws.send_json({'type': kind, 'title': 'Last saved song', 'videoId': 'v3',
                      'playlist': 'PLSESSION', 'paused': False})
        _drain_presence(ws, bridge)
        assert len(calls) == 1
        assert calls[0]['video_id'] == 'v3' and calls[0]['playlist_ytm_id'] == 'PLSESSION'
        assert time.monotonic() - bridge.now_playing_seen_at < 2


@pytest.mark.parametrize('change', [
    {'paused': True}, {'muted': True}, {'tabMuted': True}, {'volume': 0},
    {'metadataVerified': False}, {'playlist': ''}, {'videoId': ''}, {'title': ''},
])
def test_incomplete_or_inaudible_heartbeat_does_not_schedule_reattachment(change):
    bridge, calls = Bridge(), []
    app = _presence_app(bridge, lambda: calls.append(True))
    with TestClient(app).websocket_connect('/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as ws:
        ws.send_json({'type': 'now-heartbeat', 'title': 'Song', 'videoId': 'v3',
                      'playlist': 'PLSESSION', 'paused': False, **change})
        _drain_presence(ws, bridge)
        assert not calls


def test_failed_preparation_admission_keeps_receiving_transport_replies():
    bridge = Bridge()

    def closing():
        raise RuntimeError('Application is closing')

    with TestClient(_presence_app(bridge, closing)).websocket_connect(
            '/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as ws:
        ws.send_json({'type': 'now-heartbeat', 'title': 'Song', 'videoId': 'v3',
                      'playlist': 'PLSESSION', 'paused': False})
        _drain_presence(ws, bridge)
