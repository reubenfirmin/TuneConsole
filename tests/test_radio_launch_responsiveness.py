"""Confirmed playback and status must not wait for the initial direction menu."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.radio_session_support import poc


@pytest.mark.parametrize('stop', [False, True])
def test_initial_direction_planning_keeps_status_and_stop_available(poc, monkeypatch, stop):
    controller, remote, frames, _, songs = poc
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
