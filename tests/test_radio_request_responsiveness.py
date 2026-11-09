"""Radio's SQL preflight must leave the ASGI response pump available."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import (
    completed_poc as poc, completed_radio_schema, completed_radio_store,
)
from tests.store_fixture_support import without_query_timing
from yt_playlist.rec import account_growth
from yt_playlist.web.routes import radio_session
from yt_playlist.web.routes.bridge import build as bridge_routes


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """The actual ASGI preflight routes have no application background jobs."""


@pytest.mark.parametrize('phase', ['bootstrap', 'start_access', 'tune_access'])
def test_radio_preflight_leaves_status_and_unrelated_requests_available(poc, monkeypatch, phase):
    controller, remote, frames, *_ = poc
    controller.ctx.bridge.resumable_player = None
    entered, release = threading.Event(), threading.Event()
    controller.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    body = {'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id'], 'launch_id': 'held-start'}
    endpoint = '/radio/start'

    def held(target):
        def call(*args, **kwargs):
            entered.set()
            assert release.wait(5), 'The test did not release Radio preflight'
            return target(*args, **kwargs)
        return call

    if phase == 'bootstrap':
        monkeypatch.setattr(controller, 'bootstrap', held(controller.bootstrap))
    else:
        if phase == 'start_access':
            monkeypatch.setattr(account_growth, 'radio_access', held(lambda *args, **kwargs: True))
            controller.ctx.store.settings.set_setting('onboard_listening_format', 'library')
        else:
            monkeypatch.setattr(account_growth, 'retained_access', lambda *args: False)
            monkeypatch.setattr(account_growth, 'vibe_access', held(lambda *args, **kwargs: True))
            monkeypatch.setattr(account_growth, 'view', lambda *args, **kwargs: pytest.fail('Full journey read during tuning'))
            assert controller.start()['ok']
            endpoint = '/radio/tune'
            body = {'comfort': .5, 'theme': .5, 'energy': .8}

    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    app.include_router(bridge_routes(controller.ctx))

    @app.get('/unrelated')
    async def unrelated():
        return {'ok': True}

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        if phase != 'tune_access':
            # Network ordering can deliver Cancel before Start admission.
            assert client.post('/radio/stop', json={'launch_id': 'cancelled-before-admission'}).json()['ok']
            assert not client.post(endpoint, json={**body, 'launch_id': 'cancelled-before-admission'}).json()['ok']
            assert not entered.is_set() and not remote.ids
        starting = pool.submit(client.post, endpoint, json=body)
        try:
            assert entered.wait(2), 'Radio did not reach the selected preflight boundary'
            # All calls share TestClient's single ASGI event loop. A synchronous
            # preflight in an async endpoint prevents even unrelated HTTP work.
            for path in ('/unrelated', '/radio/status', '/bridge/status'):
                response = pool.submit(client.get, path).result(timeout=.5)
                assert response.status_code == 200, response.text
            if phase != 'tune_access':
                stopped = pool.submit(client.post, '/radio/stop', json={'launch_id': body['launch_id']}).result(timeout=.5)
                assert stopped.json()['ok']
                assert stopped.json()['stopping']
        finally:
            release.set()
            response = starting.result(timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()['ok'] == (phase == 'tune_access'), response.text
        if phase != 'tune_access':
            assert not remote.ids
            assert not client.get('/radio/status').json().get('stopping')
            assert not client.post(endpoint, json=body).json()['ok']
            # An old Cancel must not touch the next launch while its preflight
            # is pending, or after its session owns the confirmed handoff.
            entered.clear()
            release.clear()
            fresh = pool.submit(client.post, endpoint, json={**body, 'launch_id': 'fresh-start'})
            try:
                assert entered.wait(2)
                serial = controller.tuning_serial
                for old_id in ('cancelled-before-admission', body['launch_id']):
                    state = client.post('/radio/stop', json={'launch_id': old_id}).json()
                    assert state['ok'] and state['starting'] and not state['stopping']
                    assert controller.tuning_serial == serial
                    assert not client.post(endpoint, json={**body, 'launch_id': old_id}).json()['ok']
                    assert controller.tuning_serial == serial
            finally:
                release.set()
                response = fresh.result(timeout=5)
            assert response.json()['ok'], response.text
            session = controller.session
            assert session.launch_id == 'fresh-start'
            assert len(frames) == 1
            for old_id in ('cancelled-before-admission', body['launch_id']):
                state = client.post('/radio/stop', json={'launch_id': old_id}).json()
                assert state['ok'] and state['active'] and state['launch_id'] == 'fresh-start'
                assert controller.session is session and not session.stop.is_set()
                assert not session.stopped_by_user and len(frames) == 1
                assert controller.tuning_serial == serial
            # Identified cancellation and legacy global Stop both retain their
            # existing meaning for the current owner.
            state = client.post('/radio/stop', json={'launch_id': 'fresh-start'}).json()
            assert state['ok'] and not state['active'] and session.stopped_by_user
            assert client.post('/radio/stop').json()['ok']
    assert remote.ids


def test_access_rejection_does_not_create_a_playlist(poc, monkeypatch):
    controller, remote, frames, *_ = poc
    monkeypatch.setattr(account_growth, 'radio_access', lambda *args, **kwargs: False)
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    with TestClient(app) as client:
        response = client.post('/radio/start', json={'video_id': 'v1'})
    assert response.status_code == 409
    assert 'journey' in response.json()['detail']
    assert not remote.ids and not frames
    assert not controller.state()['active']
