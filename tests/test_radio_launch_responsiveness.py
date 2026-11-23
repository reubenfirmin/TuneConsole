"""Confirmed playback and status must not wait for the initial direction menu."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from tests.radio_session_support import (
    completed_poc as poc, completed_radio_schema, completed_radio_store,
)
from tests.store_fixture_support import without_query_timing


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Controller concurrency has no HTTP application background loop."""


@pytest.mark.parametrize('stop', [False, True])
def test_initial_direction_planning_keeps_status_and_stop_available(poc, monkeypatch, stop):
    controller, remote, frames, workers, songs = poc
    priority = controller.ctx.store.priority
    # The same request must remain cancellable before it has a Session, too.
    # Hold resolution of the selected song before the remote write.
    resolve = controller.launch.resolve_known
    loading, loaded = threading.Event(), threading.Event()

    def slow_resolution(*args, **kwargs):
        loading.set()
        assert loaded.wait(5)
        return resolve(*args, **kwargs)

    if stop:
        with monkeypatch.context() as patch:
            patch.setattr(controller.launch, 'resolve_known', slow_resolution)
            with ThreadPoolExecutor(2) as pool:
                starting = pool.submit(controller.start, songs[0]['video_id'])
                try:
                    assert loading.wait(3)
                    assert priority._foreground == 1
                    state = pool.submit(controller.state).result(.5)
                    assert state['starting'] and controller.session is None
                    assert pool.submit(controller.stop).result(.5)['ok']
                    assert not controller.status()['starting']
                finally:
                    loaded.set()
                assert not starting.result(5)['ok']
            assert not remote.ids and not frames and controller.session is None

        waiting = threading.Event()
        launch_operation = controller._launch_operation

        def entering_launch_operation(cancelled):
            waiting.set()
            return launch_operation(cancelled)

        with monkeypatch.context() as patch:
            patch.setattr(controller, '_launch_operation', entering_launch_operation)
            with ThreadPoolExecutor(2) as pool:
                controller.operation.acquire()
                starting = pool.submit(controller.start, songs[0]['video_id'])
                try:
                    assert waiting.wait(3)
                    assert pool.submit(controller.stop).result(.5)['ok']
                    assert not starting.result(.5)['ok']
                finally:
                    controller.operation.release()
            assert not remote.ids and not frames and controller.session is None

        # Once a remote write is admitted, Stop returns promptly but must expose
        # its unfinished receipt until the original launch drains. It never
        # replays that create or navigates after the confirmation arrives.
        writing, written = threading.Event(), threading.Event()
        create = remote.create_playlist

        def held_receipt(*args, **kwargs):
            result = create(*args, **kwargs)
            writing.set()
            assert written.wait(5)
            return result

        def pending_start():
            with controller.launch_request('stopped-during-create') as launch:
                return controller.start(songs[0]['video_id'], launch=launch)

        with monkeypatch.context() as patch:
            patch.setattr(remote, 'create_playlist', held_receipt)
            with ThreadPoolExecutor(2) as pool:
                starting = pool.submit(pending_start)
                try:
                    assert writing.wait(3)
                    assert priority._foreground == 1
                    assert controller.ctx.store.radio_checkpoints.pending_seeded_launch() is not None
                    stopped = pool.submit(controller.stop).result(.5)
                    assert stopped['ok'] and stopped['stopping'] and not stopped['active']
                    assert controller.state()['stopping']
                    assert remote.ids == [songs[0]['video_id']] and not frames
                finally:
                    written.set()
                assert not starting.result(5)['ok']
            assert not controller.state()['stopping'] and not frames
            assert len(remote.creations) == 1
            assert controller.ctx.store.radio_checkpoints.pending_seeded_launch() is None

    entered, release = threading.Event(), threading.Event()
    update = controller.policy.update_directions

    def slow_plan(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return update(*args, **kwargs)

    monkeypatch.setattr(controller.policy, 'update_directions', slow_plan)
    seed = songs[0]['video_id']
    assert controller.start(seed)['ok']
    assert remote.ids == [seed] and not entered.is_set()
    handoff = {'type': 'navigate', 'url': f'https://music.youtube.com/watch?v={seed}&list=PLSESSION'}
    assert frames == [handoff]
    session = controller.session
    assert not session.playback_confirmed and session.wake.is_set()
    (monitor,), monitor_args = next(worker for worker in workers if worker[1]['name'] == 'radio-session')
    bridge = controller.ctx.bridge
    bridge.now_playing = {'video_id': seed, 'playlist_ytm_id': 'PLSESSION',
                          'paused': False, 'position': 1, 'duration': 300}
    bridge.now_playing_seen_at = time.monotonic()

    # Run the scheduled monitor for one observation, then end its polling loop.
    connected = iter((True, False))

    def poll(_timeout):
        bridge.connected = next(connected)

    monkeypatch.setattr(session.wake, 'wait', poll)
    with ThreadPoolExecutor(2) as pool:
        observing = pool.submit(monitor, *monitor_args['args'])
        try:
            assert entered.wait(3)
            assert priority._foreground == 0
            assert session.playback_confirmed and session.furthest == 0
            assert frames == [handoff]
            original = session.directions
            state = pool.submit(controller.status).result(.5)
            assert state['playlist_id'] == 'PLSESSION' and not state['directions']
            if stop:
                assert pool.submit(controller.stop).result(.5)['ok']
        finally:
            release.set()
        observing.result(5)
    assert frames == [handoff] and session.error is None
    if stop:
        assert session.directions is original
        assert not controller.status()['directions']
    else:
        assert session.directions is not original
        assert controller.status()['directions']
