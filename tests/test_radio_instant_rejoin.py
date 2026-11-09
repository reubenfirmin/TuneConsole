"""An established saved session rejoins without preparing another candidate model."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import (
    completed_poc as poc, completed_radio_schema, completed_radio_store, play,
    controlled_radio_checkpoints,
)
from tests.store_fixture_support import without_query_timing
from tests.template_support import web_templates
from yt_playlist.rec.radio_restored import RestoredCandidates
from yt_playlist.web import radio_session
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.routes.radio_session import build as radio_routes


pytestmark = pytest.mark.usefixtures('without_query_timing', 'controlled_radio_checkpoints')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These controllers and route-only apps have no periodic application jobs."""


def saved_session(poc):
    controller, remote, frames, *_ = poc
    assert controller.start()['ok']
    play(controller, 'v2')
    controller.suspend()
    controller._persist(controller.session)
    return controller, remote, frames


def test_verified_saved_session_is_useful_before_controller_reattachment(poc):
    controller, *_ = saved_session(poc)
    restored = PlaylistRadio(controller.ctx)
    state = restored.bootstrap()['state']
    assert state['active'] and state['tracks'] and state['directions']
    assert not state.get('resolving'), 'Confirmed saved playback must not wait for model preparation'


def test_rejoin_and_saved_direction_selection_do_not_build_candidates(poc, monkeypatch):
    controller, remote, frames = saved_session(poc)
    before = controller.status()
    remote_before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))

    def forbidden(*args, **kwargs):
        raise AssertionError('Rejoining must not rebuild the candidate model')

    monkeypatch.setattr(radio_session, 'RadioCandidates', forbidden)
    restored = PlaylistRadio(controller.ctx)
    state = restored.join()
    assert state['active'] and not state['starting']
    assert state['history'] == before['history']
    assert state['directions'] == before['directions']
    assert state['menu_id'] == before['menu_id']
    restored._refresh_now(restored.session)
    assert not restored.session.now_refreshing
    branch = state['directions'][2]
    result = restored.select(branch['id'], state['menu_id'])
    assert result['ok'] and result['pending_slot'] == branch['slot']
    assert restored.persistence.load()['requested_direction']['choice']['id'] == branch['id']
    assert restored.stop()['ok']
    assert (remote.ids, remote.adds, remote.removes, frames) == remote_before


@pytest.mark.parametrize('action', ['stop', 'select'])
def test_future_candidate_loading_does_not_block_the_restored_menu(poc, monkeypatch, action):
    controller, remote, frames = saved_session(poc)
    factory = radio_session.RadioCandidates
    entered, release = threading.Event(), threading.Event()

    def held_factory(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Candidate loading was not released'
        return factory(*args, **kwargs)

    monkeypatch.setattr(radio_session, 'RadioCandidates', held_factory)
    restored = PlaylistRadio(controller.ctx)
    state = restored.join()
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))
    with ThreadPoolExecutor(max_workers=2) as pool:
        loading = pool.submit(restored._refresh_candidates, restored.session)
        try:
            assert entered.wait(3)
            live = pool.submit(restored.status).result(timeout=.5)
            assert live['directions'] == state['directions']
            if action == 'stop':
                assert pool.submit(restored.stop).result(timeout=.5)['ok']
            else:
                branch = live['directions'][2]
                result = pool.submit(restored.select, branch['id'], live['menu_id']).result(timeout=.5)
                assert result['ok'] and result['pending_slot'] == branch['slot']
        finally:
            release.set()
            loading.result(timeout=5)
    assert (remote.ids, remote.adds, remote.removes, frames) == before


def test_failed_candidate_loading_preserves_saved_metadata_and_can_retry():
    track = {'key': 'saved|artist', 'video_id': 'saved'}
    calls = []
    model = SimpleNamespace(modes={'current': {}}, retain=lambda tracks: calls.append(tracks))

    def factory(*args, **kwargs):
        calls.append('load')
        if calls == ['load']:
            raise RuntimeError('Preparation interrupted')
        return model

    restored = RestoredCandidates(object(), lambda: 1, {'fingerprint': 'published'}, factory)
    restored.retain([track])
    with pytest.raises(RuntimeError, match='Preparation interrupted'):
        _ = restored.modes
    assert not restored.model_ready and restored.fingerprint == 'published'
    assert restored.by_video == {'saved': track}
    assert restored.modes == {'current': {}} and restored.model_ready
    assert calls == ['load', 'load', [track]]
    assert calls[-1][0] is not track, 'Hydration must not mutate the published track metadata'


@pytest.mark.parametrize('action', ['stop', 'select', 'seen', 'history'])
def test_saved_view_controls_work_before_background_reattachment(poc, monkeypatch, action):
    controller, remote, frames = saved_session(poc)
    before = (list(remote.ids), list(remote.adds), list(remote.removes), list(frames))

    def forbidden(*args, **kwargs):
        raise AssertionError('Saved controls must not build the candidate model')

    monkeypatch.setattr(radio_session, 'RadioCandidates', forbidden)
    restored = PlaylistRadio(controller.ctx)
    state = restored.bootstrap()['state']
    assert restored.session is None and state['active']
    branch = state['directions'][2]
    if action == 'stop':
        assert restored.stop()['ok']
        assert restored.persistence.load()['stopped']
    elif action == 'select':
        result = restored.select(branch['id'], state['menu_id'])
        assert result['ok'] and result['pending_slot'] == branch['slot']
    elif action == 'seen':
        assert restored.seen(state['menu_id'])['ok']
    else:
        controller.ctx.radio = restored
        controller.ctx.templates = web_templates()
        app = FastAPI()
        app.include_router(radio_routes(controller.ctx))
        with TestClient(app) as client:
            response = client.get('/radio/playlist')
        assert response.status_code == 200
        assert all(f'data-vid="{track["video_id"]}"' in response.text for track in state['history'])
        assert restored.session is None
    assert (remote.ids, remote.adds, remote.removes, frames) == before
