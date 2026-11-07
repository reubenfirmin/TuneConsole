"""Joined Radio starts share one creation and one effective playback handoff."""
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.radio_session_support import poc
from yt_playlist.library import playlist_generated
from yt_playlist.web.routes import radio_session


@pytest.mark.parametrize('pending_readback', [False, True])
@pytest.mark.parametrize('newer_play', [False, True])
def test_joined_start_adopts_unsent_handoff_without_overriding_newer_play(
        poc, monkeypatch, pending_readback, newer_play):
    controller, remote, frames, _, _ = poc
    controller.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    body = {'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id'],
            'launch_id': 'current-launch'}
    entered, release, joined_intent = threading.Event(), threading.Event(), threading.Event()
    creates, intents = [], []
    create = playlist_generated.create_generated_playlist
    begin = controller.ctx.bridge.begin_playback_intent
    read = remote.get_playlist
    direct = {'type': 'navigate', 'url': 'https://music.youtube.com/watch?v=newer'}

    def held_create(*args, **kwargs):
        creates.append(args[1])
        entered.set()
        assert release.wait(10)
        return create(*args, **kwargs)

    def reserve():
        intent = begin()
        intents.append(intent)
        if len(intents) == 2:
            joined_intent.set()
        return intent

    monkeypatch.setattr(playlist_generated, 'create_generated_playlist', held_create)
    monkeypatch.setattr(controller.ctx.bridge, 'begin_playback_intent', reserve)
    if pending_readback:
        monkeypatch.setattr(remote, 'get_playlist', lambda *args, **kwargs: {
            'tracks': [{'videoId': 'v1'}], 'trackCount': 1})
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    with TestClient(app) as client, ThreadPoolExecutor(2) as workers:
        assert client.post('/radio/stop', json={'launch_id': 'old-launch'}).json()['ok']
        first = workers.submit(client.post, '/radio/start', json=body)
        second = None
        try:
            assert entered.wait(5)
            second = workers.submit(client.post, '/radio/start', json=body)
            assert joined_intent.wait(5)
            serial = controller.tuning_serial
            state = client.post('/radio/stop', json={'launch_id': 'old-launch'}).json()
            assert state['ok'] and not state['stopped'] and not state['stopping']
            assert controller.session.launch_id == 'current-launch'
            assert not controller.session.stop.is_set()
            assert controller.tuning_serial == serial
            if newer_play:
                assert controller.ctx.bridge.send_control(direct, intent=begin())
        finally:
            release.set()
        responses = [first.result(10), second.result(10)]
    assert all(response.status_code == 200 and response.json()['ok'] for response in responses)
    assert sum(bool(response.json().get('joined')) for response in responses) == 1
    assert len(creates) == 1
    if pending_readback:
        assert frames == ([direct] if newer_play else [])
        monkeypatch.setattr(remote, 'get_playlist', read)
        controller._complete_start(controller.session)
    assert frames == ([direct] if newer_play else [{
        'type': 'navigate', 'url': 'https://music.youtube.com/watch?v=v1&list=PLSESSION'}])
    assert controller.persistence.load()['playlist'] == 'PLSESSION'


def test_join_after_accepted_handoff_does_not_send_again(poc):
    controller, _, frames, _, _ = poc
    assert controller.start()['ok']
    assert controller.start()['joined']
    assert len(frames) == 1
