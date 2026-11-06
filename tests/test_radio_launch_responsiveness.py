"""Confirmed playback and status must not wait for the initial direction menu."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.radio_session_support import poc
from yt_playlist.library import playlist_generated
from yt_playlist.web import radio_session


@pytest.mark.parametrize('stop', [False, True])
def test_initial_direction_planning_keeps_status_and_stop_available(poc, monkeypatch, stop):
    controller, remote, frames, _, songs = poc
    # The same request must remain cancellable before it has a Session, too.
    # Hold the constructor and prove that Stop prevents the later remote write.
    candidates = radio_session.RadioCandidates
    loading, loaded = threading.Event(), threading.Event()

    def slow_candidates(*args, **kwargs):
        loading.set()
        assert loaded.wait(5)
        return candidates(*args, **kwargs)

    if stop:
        with monkeypatch.context() as patch:
            patch.setattr(radio_session, 'RadioCandidates', slow_candidates)
            with ThreadPoolExecutor(2) as pool:
                starting = pool.submit(controller.start, songs[0]['video_id'])
                try:
                    assert loading.wait(3)
                    state = pool.submit(controller.state).result(.5)
                    assert state['starting'] and controller.session is None
                    assert pool.submit(controller.stop).result(.5)['ok']
                    assert not controller.status()['starting']
                finally:
                    loaded.set()
                assert not starting.result(5)['ok']
            assert not remote.ids and not frames and controller.session is None

        waiting = threading.Event()
        join = controller.join

        def entering_join(**kwargs):
            waiting.set()
            return join(**kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(controller, 'join', entering_join)
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
        create = playlist_generated.create_generated_playlist

        def held_receipt(*args, **kwargs):
            result = create(*args, **kwargs)
            writing.set()
            assert written.wait(5)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(playlist_generated, 'create_generated_playlist', held_receipt)
            with ThreadPoolExecutor(2) as pool:
                starting = pool.submit(controller.start, songs[0]['video_id'])
                try:
                    assert writing.wait(3)
                    stopped = pool.submit(controller.stop).result(.5)
                    assert stopped['ok'] and stopped['stopping'] and not stopped['active']
                    assert controller.state()['stopping'] and remote.ids and not frames
                finally:
                    written.set()
                assert not starting.result(5)['ok']
            assert not controller.state()['stopping'] and not frames

    entered, release = threading.Event(), threading.Event()
    update = controller.policy.update_directions

    def slow_plan(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return update(*args, **kwargs)

    monkeypatch.setattr(controller.policy, 'update_directions', slow_plan)
    with ThreadPoolExecutor(2) as pool:
        starting = pool.submit(controller.start, songs[0]['video_id'])
        try:
            assert entered.wait(3)
            assert len(frames) == 1 and 'list=PLSESSION' in frames[0]['url']
            original = controller.session.directions
            state = pool.submit(controller.status).result(.5)
            assert state['playlist_id'] == 'PLSESSION' and not state['directions']
            if stop:
                assert pool.submit(controller.stop).result(.5)['ok']
        finally:
            release.set()
        assert starting.result(5)['ok']
    if stop:
        assert controller.session.directions is original
    else:
        assert controller.session.directions is not original
