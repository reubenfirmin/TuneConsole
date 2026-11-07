"""Radio planning must keep status, Stop and foreground readers available."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc, play
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.routes.bridge import build as bridge_routes


@pytest.mark.parametrize('stop', [False, True])
def test_ongoing_direction_planning_keeps_bridge_status_and_readers_available(poc, monkeypatch, stop):
    controller, remote, frames, *_ = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    controller.suspend()
    controller.session.candidates.fingerprint = 'previous-publication'
    controller._persist(controller.session)
    restored = PlaylistRadio(controller.ctx)
    controller.ctx.radio = restored
    controller.ctx.bridge.resumable_player = None
    assert restored.join()['active']
    entered, release = threading.Event(), threading.Event()
    update = RadioDirections.update

    def held_plan(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Direction planning was not released'
        return update(*args, **kwargs)

    monkeypatch.setattr(RadioDirections, 'update', held_plan)
    app = FastAPI()
    app.include_router(bridge_routes(controller.ctx))
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=6) as pool:
        planning = pool.submit(restored.listening.update, restored.session, force=True)
        try:
            assert entered.wait(3), 'Ongoing update did not reach direction planning'
            original = restored.session.directions
            published = original.snapshot()
            polls = [pool.submit(client.get, '/bridge/status') for _ in range(4)]
            for poll in polls:
                response = poll.result(timeout=.5)
                assert response.status_code == 200, response.text
                assert response.json()['radio']
            with controller.ctx.store.read_view() as reader:
                assert reader.accounts.get_master_account() is not None
            assert vars(original) == vars(published)
            if stop:
                assert pool.submit(restored.stop).result(timeout=.5)['ok']
        finally:
            release.set()
            planning.result(timeout=5)
    assert restored.session.stopped_by_user is stop
    assert (restored.session.directions is original) is stop
    assert (remote.ids, remote.adds, remote.removes, frames) == before


@pytest.mark.parametrize('change', ['stop', 'selection', 'feedback', 'history', 'failure'])
def test_late_direction_plan_preserves_published_state_and_newer_actions(poc, monkeypatch, change):
    controller, _, _, _, songs = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    session = controller.session
    original = session.directions
    published = original.snapshot()
    entered, release = threading.Event(), threading.Event()
    update = controller.policy.update_directions

    def held_plan(view, history, **kwargs):
        update(view, history, **kwargs)
        entered.set()
        assert release.wait(5), 'Direction planning was not released'
        if change == 'failure':
            raise RuntimeError('Planning failed after building a private menu')

    monkeypatch.setattr(controller.policy, 'update_directions', held_plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        planning = pool.submit(controller.listening.update, session, force=True)
        try:
            assert entered.wait(3)
            state = pool.submit(controller.status).result(timeout=.5)
            assert state['menu_id'] == published.menu_id
            assert vars(original) == vars(published)
            if change == 'stop':
                assert pool.submit(controller.stop).result(timeout=.5)['ok']
            elif change == 'selection':
                branch = state['directions'][2]
                result = pool.submit(controller.select, branch['id'], state['menu_id']).result(timeout=.5)
                assert result['ok'] and result['pending_slot'] == branch['slot']
            elif change == 'feedback':
                with controller.lock:
                    controller.listening.reject(session, songs[10])
                    session.feedback_dirty = True
            elif change == 'history':
                pool.submit(controller.listening.record, session, 2).result(timeout=.5)
            expected = original.snapshot()
            requested = session.requested_direction
        finally:
            release.set()
        if change == 'failure':
            with pytest.raises(RuntimeError, match='Planning failed'):
                planning.result(timeout=5)
        else:
            planning.result(timeout=5)
    assert session.directions is original
    assert vars(original) == vars(expected)
    assert session.requested_direction is requested
    if change == 'feedback':
        assert session.feedback_dirty and songs[10]['video_id'] in original.blocked


def test_live_radio_projection_needs_no_reader_slot(poc):
    from contextlib import ExitStack

    controller, *_ = poc
    assert controller.start()['ok']
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(controller.ctx.store.read_view())
        assert controller.state()['playlist_id'] == 'PLSESSION'


@pytest.mark.parametrize('change', ['none', 'stop', 'selection', 'feedback', 'history', 'menu', 'failure'])
def test_tuning_plans_privately_without_blocking_status_or_newer_actions(poc, monkeypatch, change):
    controller, remote, frames, _, songs = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    session = controller.session
    original, candidates = session.directions, session.candidates
    published = original.snapshot()
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    entered, release = threading.Event(), threading.Event()
    update = RadioDirections.update

    calls = []
    def held_plan(*args, **kwargs):
        result = update(*args, **kwargs)
        calls.append(1)
        entered.set()
        assert release.wait(5), 'Tuning computation was not released'
        if change == 'failure':
            raise RuntimeError('Tuning failed after building a private menu')
        return result

    monkeypatch.setattr(RadioDirections, 'update', held_plan)
    app = FastAPI()
    controller.ctx.bridge.resumable_player = None
    app.include_router(bridge_routes(controller.ctx))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as pool:
        tuning = pool.submit(controller.tune, {'comfort': .1, 'theme': .9, 'energy': .2})
        try:
            assert entered.wait(3)
            state = pool.submit(controller.status).result(timeout=.5)
            response = pool.submit(client.get, '/bridge/status').result(timeout=.5)
            assert response.status_code == 200 and response.json()['radio']
            assert state['menu_id'] == published.menu_id
            assert session.candidates is candidates
            assert vars(original) == vars(published)
            if change == 'stop':
                assert pool.submit(controller.stop).result(timeout=.5)['ok']
            elif change == 'selection':
                branch = state['directions'][2]
                assert pool.submit(controller.select, branch['id'], state['menu_id']).result(timeout=.5)['ok']
            elif change == 'feedback':
                with controller.lock:
                    controller.listening.reject(session, songs[10])
                    session.feedback_dirty = True
            elif change == 'history':
                pool.submit(controller.listening.record, session, 2).result(timeout=.5)
            elif change == 'menu':
                menu = original.snapshot()
                update(menu, list(session.heard), [], session.transition, force=True)
                with controller.lock:
                    session.directions = menu
            expected, requested = original.snapshot(), session.requested_direction
        finally:
            release.set()
        if change == 'failure':
            with pytest.raises(RuntimeError, match='Tuning failed'):
                tuning.result(timeout=5)
        else:
            assert tuning.result(timeout=5)['ok'] is (change in {'none', 'feedback', 'history', 'menu'})
    if change in {'none', 'feedback', 'history', 'menu'}:
        assert session.candidates is not candidates and session.directions is not original
        assert session.requested_direction is not None
        assert session.candidates.tuning == {'comfort': .1, 'theme': .9, 'energy': .2}
        if change != 'none':
            assert len(calls) == 2
        if change == 'feedback':
            assert session.feedback_dirty and songs[10]['video_id'] in session.directions.blocked
        if change == 'history':
            assert session.heard[-1]['video_id'] == 'v3'
            assert all(t['video_id'] != 'v3' for branch in session.directions.slots for t in branch['tracks'])
    else:
        assert session.candidates is candidates and session.directions is original
        assert vars(original) == vars(expected)
        assert session.requested_direction is requested
    assert (remote.ids, remote.adds, remote.removes, frames) == before
