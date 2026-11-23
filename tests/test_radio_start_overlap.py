"""Joined Radio starts share one creation and one effective playback handoff."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from tests.radio_session_support import (
    controlled_radio_checkpoints, play, radio_controller, seed_completed_pool,
)
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, use_native_sql_calls, without_query_timing,
)
from yt_playlist.library import generated_acquisition
from yt_playlist.web.routes import radio_session


pytestmark = pytest.mark.usefixtures('without_query_timing', 'controlled_radio_checkpoints')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """The route-only app has no periodic application jobs."""


@pytest.fixture(scope='module')
def radio_schema():
    # Confirmed creation records measurement through a real captured snapshot.
    # Keep its revision guards alongside actual SQLite admission and WAL.
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def poc(tmp_path, radio_schema, monkeypatch):
    with _file_consumer_store(tmp_path, radio_schema) as store:
        with monkeypatch.context() as inputs:
            use_native_sql_calls(inputs)
            songs = seed_completed_pool(store)
        with radio_controller(monkeypatch, store, songs) as state:
            yield state


@pytest.mark.parametrize('pending_readback', [False, True])
@pytest.mark.parametrize('newer_play', [False, True])
def test_overlapping_retry_preserves_original_handoff_and_newer_play(
        poc, monkeypatch, pending_readback, newer_play):
    controller, remote, frames, _, _ = poc
    store = controller.ctx.store
    body = {'launch_id': 'current-launch'}
    if pending_readback:
        # A saved queue still waits for complete readback before its handoff.
        store.rec_proposals.put_proposals('listening_choices', {
            'home_format': {'episodes': {'radio': 1}}}, 1000)
        account = store.accounts.get_master_account().id
        videos = ['v1', 'v2', 'v3']
        by_video = store.tracks.track_ids_for_videos(videos)
        playlist = store.playlists.upsert_playlist(account, 'PLSOURCE', 'My mix', 3, '', 1)
        store.playlists.set_playlist_tracks(playlist, [by_video[video] for video in videos])
        body['playlist_id'] = playlist
        owner, method = generated_acquisition, 'create_generated_playlist'
    else:
        store.settings.set_setting('onboard_listening_format', 'radio')
        offer = controller.seeds()
        body.update(video_id=offer['tracks'][0]['video_id'], menu_id=offer['menu_id'])
        owner, method = remote, 'create_playlist'
    entered, release, replay_admitted = threading.Event(), threading.Event(), threading.Event()
    intents = []
    create = getattr(owner, method)
    begin = controller.ctx.bridge.begin_playback_intent
    launch_request = controller.launch_request
    read = remote.get_playlist
    direct = {'type': 'navigate', 'url': 'https://music.youtube.com/watch?v=newer'}

    def held_create(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        if not pending_readback:
            assert kwargs['video_ids'] == [body['video_id']]
        return create(*args, **kwargs)

    def reserve():
        intent = begin()
        intents.append(intent)
        return intent

    @contextmanager
    def observe_admission(launch_id):
        with launch_request(launch_id) as launch:
            if launch.replay:
                replay_admitted.set()
            yield launch

    monkeypatch.setattr(owner, method, held_create)
    monkeypatch.setattr(controller.ctx.bridge, 'begin_playback_intent', reserve)
    monkeypatch.setattr(controller, 'launch_request', observe_admission)
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
            serial = controller.tuning_serial
            second = workers.submit(client.post, '/radio/start', json=body)
            assert replay_admitted.wait(5)
            assert len(intents) == 1 and controller.tuning_serial == serial
            assert not frames and not remote.creations
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
    assert len(remote.creations) == 1
    recovery = store.radio_checkpoints.pending_seeded_launch()
    if pending_readback or newer_play:
        assert recovery is None
    else:
        assert recovery['launch_id'] == body['launch_id'] and recovery['dispatch_started']
    if pending_readback:
        assert frames == ([direct] if newer_play else [])
        monkeypatch.setattr(remote, 'get_playlist', read)
        controller._complete_start(controller.session)
        assert not controller.session.initial_tracks and len(remote.creations) == 1
    assert frames == ([direct] if newer_play else [{
        'type': 'navigate', 'url': 'https://music.youtube.com/watch?v=v1&list=PLSESSION'}])
    if not pending_readback:
        assert remote.ids == ['v1']
        if not newer_play:
            play(controller, 'v1')
            assert remote.ids == ['v1', 'v2', 'v3'] and len(frames) == 1
            assert store.radio_checkpoints.pending_seeded_launch() is None
    if pending_readback or not newer_play:
        assert controller.persistence.load()['playlist'] == 'PLSESSION'


def test_join_after_accepted_handoff_does_not_send_again(poc):
    controller, _, frames, _, _ = poc
    assert controller.start()['ok']
    assert controller.start()['joined']
    assert len(frames) == 1


def test_disconnected_start_request_retains_ownership_until_worker_finishes(poc, monkeypatch):
    controller, remote, frames, *_ = poc
    controller.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    body = {'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id'],
            'launch_id': 'disconnected-request'}
    entered, release, replay_admitted = threading.Event(), threading.Event(), threading.Event()
    resolve = controller.launch.resolve_known
    replay = controller.replay_start

    def held_resolve(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return resolve(*args, **kwargs)

    def observe_replay(launch):
        replay_admitted.set()
        return replay(launch)

    monkeypatch.setattr(controller.launch, 'resolve_known', held_resolve)
    monkeypatch.setattr(controller, 'replay_start', observe_replay)
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/radio/start', json=body))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                serial = controller.tuning_serial
                (admission,) = controller.launch_requests.values()
                assert admission.launch_id == body['launch_id']
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                retry = asyncio.create_task(client.post('/radio/start', json=body))
                assert await asyncio.to_thread(replay_admitted.wait, 5)
                assert controller.tuning_serial == serial
                assert controller.launch_requests.get(admission.cancelled) is admission
                assert not frames and not remote.creations
            finally:
                release.set()
            response = await asyncio.wait_for(retry, 10)
            assert response.json()['ok'] and response.json()['joined']

    asyncio.run(requests())
    assert len(remote.creations) == 1 and remote.ids == [body['video_id']]
    assert frames == [{'type': 'navigate',
                       'url': f"https://music.youtube.com/watch?v={body['video_id']}&list=PLSESSION"}]
    assert not controller.launch_requests
