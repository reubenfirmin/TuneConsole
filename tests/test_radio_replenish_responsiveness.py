"""A slow continuation must not block Radio status or an explicit Stop."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc, play
from yt_playlist.web.routes.bridge import build as bridge_routes


@pytest.mark.parametrize('stage', ['song-handoff', 'genre-recheck'])
def test_stop_and_status_remain_available_during_stream_refill(poc, monkeypatch, stage):
    controller, remote, frames, *_ = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    session = controller.session
    state = controller.status()
    branch = state['directions'][1]
    target = branch['tracks'][-1]['video_id']
    controller.ctx.bridge.auto_adopt = False
    assert controller.select(branch['id'], state['menu_id'], target)['ok']

    # A changed current genre can invalidate an already queued choice. The
    # other case follows the ordinary confirmed song handoff and short tail.
    if stage == 'genre-recheck':
        monkeypatch.setattr(controller.policy, 'genre_mismatch', lambda *args: True)
    entered, release = threading.Event(), threading.Event()
    replenish = controller.policy.replenish

    def held_refill(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Stream refill was not released'
        return replenish(*args, **kwargs)

    monkeypatch.setattr(controller.policy, 'replenish', held_refill)
    controller.ctx.bridge.resumable_player = None
    app = FastAPI()
    app.include_router(bridge_routes(controller.ctx))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as pool:
        advancing = pool.submit(controller.advance, session)
        try:
            assert entered.wait(3), 'The selected workflow did not reach stream refill'
            before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
            current = pool.submit(controller.status).result(timeout=.5)
            assert current['active'] and current['playlist_id'] == 'PLSESSION'
            response = pool.submit(client.get, '/bridge/status').result(timeout=.5)
            assert response.status_code == 200 and response.json()['radio']
            assert pool.submit(controller.stop).result(timeout=.5)['ok']
            assert controller.persistence.load()['stopped']
        finally:
            release.set()
            advancing.result(timeout=5)
    assert session.stopped_by_user
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('stage', ['song-handoff', 'genre-recheck'])
@pytest.mark.parametrize('change', ['selection', 'feedback', 'history', 'failure'])
def test_late_refill_preserves_the_menu_journal_and_newer_actions(poc, monkeypatch, stage, change):
    controller, remote, frames, _, songs = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    session = controller.session
    state = controller.status()
    branch = state['directions'][1]
    controller.ctx.bridge.auto_adopt = False
    assert controller.select(branch['id'], state['menu_id'], branch['tracks'][-1]['video_id'])['ok']
    if stage == 'genre-recheck':
        monkeypatch.setattr(controller.policy, 'genre_mismatch', lambda *args: True)
    entered, release = threading.Event(), threading.Event()
    replenish = controller.policy.replenish

    def held_refill(*args, **kwargs):
        result = replenish(*args, **kwargs)
        entered.set()
        assert release.wait(5), 'Stream refill was not released'
        if change == 'failure':
            raise RuntimeError('Continuation failed after computing its private result')
        return result

    monkeypatch.setattr(controller.policy, 'replenish', held_refill)
    with ThreadPoolExecutor(max_workers=2) as pool:
        advancing = pool.submit(controller.advance, session)
        try:
            assert entered.wait(3)
            original = session.directions
            before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
            state = pool.submit(controller.status).result(timeout=.5)
            if change == 'selection':
                latest = state['directions'][2]
                assert pool.submit(controller.select, latest['id'], state['menu_id'],
                                   latest['tracks'][-1]['video_id']).result(timeout=.5)['ok']
            elif change == 'feedback':
                with controller.lock:
                    controller.listening.reject(session, songs[10])
                    session.feedback_dirty = True
            elif change == 'history':
                pool.submit(controller.listening.record, session, 0).result(timeout=.5)
            expected = original.snapshot()
            requested, replacement = session.requested_direction, session.replacement
            history = session.history_view()
        finally:
            release.set()
        if change == 'failure':
            with pytest.raises(RuntimeError, match='Continuation failed'):
                advancing.result(timeout=5)
        else:
            advancing.result(timeout=5)
    assert session.directions is original and vars(original) == vars(expected)
    assert session.requested_direction is requested and session.replacement is replacement
    assert session.history_view() == history
    assert (remote.ids, remote.adds, remote.removes, frames) == before
    if replacement:
        assert replacement.saved and replacement.waiting is None
        assert replacement.play['sent'], 'Confirmed playback ownership must survive a discarded refill'
