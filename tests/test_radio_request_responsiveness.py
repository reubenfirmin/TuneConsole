"""Radio's SQL preflight must leave the ASGI response pump available."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.radio_session_support import poc
from yt_playlist.rec import account_growth
from yt_playlist.web.routes import radio_session
from yt_playlist.web.routes.bridge import build as bridge_routes


@pytest.mark.parametrize('phase', ['bootstrap', 'start_access', 'tune_access'])
def test_radio_preflight_leaves_status_and_unrelated_requests_available(poc, monkeypatch, phase):
    controller, remote, *_ = poc
    controller.ctx.bridge.resumable_player = None
    entered, release = threading.Event(), threading.Event()
    controller.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    body = {'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id']}
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
            monkeypatch.setattr(account_growth, 'view', held(lambda *args, **kwargs: {
                'ready_areas': {'radio': True}, 'vibes_ready': True,
            }))
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
        starting = pool.submit(client.post, endpoint, json=body)
        try:
            assert entered.wait(2), 'Radio did not reach the selected preflight boundary'
            # All calls share TestClient's single ASGI event loop. A synchronous
            # preflight in an async endpoint prevents even unrelated HTTP work.
            for path in ('/unrelated', '/radio/status', '/bridge/status'):
                response = pool.submit(client.get, path).result(timeout=.5)
                assert response.status_code == 200, response.text
        finally:
            release.set()
            response = starting.result(timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()['ok'], response.text
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
