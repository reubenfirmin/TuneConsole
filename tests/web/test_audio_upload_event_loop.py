"""Audio publication must keep serving requests and remain owned after cancellation."""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_audio_vibes import sample
from yt_playlist.rec import audio_vibe_prediction
from yt_playlist.web.app import create_app


def _upload(store, endpoint):
    value = sample()
    if endpoint == 'runs':
        return {key: value[key] for key in ('run_id', 'video_id', 'title', 'artist')}
    if endpoint == 'feedback':
        store.audio_vibes.save_sample(value, 1000)
        return {'run_id': value['run_id'], 'sample_ids': [value['id']], 'vibes': ['cruise'],
                'taxonomy_version': audio_vibe_prediction.TAXONOMY_VERSION}
    return value


@pytest.mark.parametrize('endpoint,phase', [
    ('runs', 'lock'), ('samples', 'lock'), ('feedback', 'lock'),
    ('samples', 'publication'), ('feedback', 'publication'), ('runs', 'response'),
])
def test_audio_upload_waits_and_response_preparation_keep_serving(
        file_backed_store, monkeypatch, endpoint, phase):
    store = file_backed_store
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1' if endpoint == 'feedback' or phase == 'response' else '0')
    value = _upload(store, endpoint)
    store.settings.set_setting('audio-upload-probe', 'committed')
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    release = threading.Event()

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    @app.get('/_test/committed-read')
    def committed():
        return {'value': store.settings.get_setting('audio-upload-probe')}

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = loop.create_future()
        gate_expired = threading.Event()

        def signal_entry():
            if not entered.done():
                entered.set_result(None)

        def wait():
            loop.call_soon_threadsafe(signal_entry)
            if not release.wait(5):
                gate_expired.set()

        holder = None
        if phase == 'lock':
            def hold():
                with store._lock:
                    wait()

            holder = threading.Thread(target=hold)
            holder.start()
        else:
            owner, name = ((audio_vibe_prediction, 'predict') if phase == 'response' else
                           (store.audio_vibes.repository if endpoint == 'feedback'
                            else store.audio_vibes.sample_files,
                            'save_annotation' if endpoint == 'feedback' else 'publish_reserved'))
            original = getattr(owner, name)

            def delayed(*args, **kwargs):
                wait()
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, name, delayed)

        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://127.0.0.1') as client:
                if holder is not None:
                    await asyncio.wait((entered,), timeout=10)
                    assert entered.done(), 'The store lock was not held'
                request = asyncio.create_task(client.post('/audio-vibes/' + endpoint, json=value))
                try:
                    if holder is not None:
                        await asyncio.sleep(.05)
                    else:
                        # Admission can take time. Start measuring only after the
                        # publication or predictor reaches its hold.
                        await asyncio.wait((entered, request), timeout=10, return_when=asyncio.FIRST_COMPLETED)
                        assert entered.done(), f'The upload did not reach {phase}'
                    started = time.monotonic()
                    for path in ('/_test/event-loop', '/bridge/status', '/_test/committed-read', '/static/app.css'):
                        requested = time.monotonic()
                        response = await asyncio.wait_for(client.get(path), 1)
                        assert response.status_code == 200, (path, response.text)
                        assert time.monotonic() - requested < 1, path
                        if path == '/_test/committed-read':
                            assert response.json() == {'value': 'committed'}
                    assert time.monotonic() - started < 1
                    assert not gate_expired.is_set(), 'The probes must finish while the audio work is held'
                    assert not request.done(), 'The upload did not exercise the blocked worker'
                    assert 'persist_audio' in app.state.ctx.work.active
                finally:
                    release.set()
                    response = await asyncio.wait_for(request, 5)
                assert response.status_code == 200, response.text
                assert response.json()['run_id'] == value['run_id']
        finally:
            release.set()
            if holder is not None:
                holder.join(1)
                assert not holder.is_alive()

    asyncio.run(exercise())
    assert not app.state.ctx.work.active


@pytest.mark.parametrize('endpoint', ['samples', 'feedback'])
def test_cancelled_audio_upload_remains_tracked_and_retry_is_idempotent(file_backed_store, monkeypatch, endpoint):
    store = file_backed_store
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1' if endpoint == 'feedback' else '0')
    value = _upload(store, endpoint)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    release = threading.Event()
    owner = store.audio_vibes.repository if endpoint == 'feedback' else store.audio_vibes.sample_files
    name = 'save_annotation' if endpoint == 'feedback' else 'publish_reserved'
    publish = getattr(owner, name)
    work = app.state.ctx.work

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = loop.create_future()

        def delayed(*args, **kwargs):
            if not release.is_set():
                loop.call_soon_threadsafe(entered.set_result, None)
                assert release.wait(10), 'The publication hold expired before cancellation'
            return publish(*args, **kwargs)

        monkeypatch.setattr(owner, name, delayed)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            request = asyncio.create_task(client.post('/audio-vibes/' + endpoint, json=value))
            try:
                await asyncio.wait((entered, request), timeout=10, return_when=asyncio.FIRST_COMPLETED)
                if request.done():
                    response = request.result()
                    phase = 'after reaching the hold' if entered.done() else f'before reaching {name}'
                    pytest.fail(f'{endpoint} returned HTTP {response.status_code} {phase}: {response.text}')
                assert entered.done(), f'{endpoint} did not reach {name} in 10 seconds. Active work: {work.active}'
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert 'persist_audio' in work.active
                work.close_admission('restart')
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
                assert await asyncio.to_thread(work.wait, 5)
                work.resume_restart()
            first = store.audio_vibes.samples(value['run_id'])
            assert len(first) == 1
            def annotations_by_kind():
                return dict(store.conn.execute('SELECT kind,count(*) FROM audio_annotations '
                                               'WHERE run_key=? GROUP BY kind', (value['run_id'],)))

            expected = {'playback': 1, 'session': 1} if endpoint == 'feedback' else {'playback': 1}
            assert annotations_by_kind() == expected
            retry = await client.post('/audio-vibes/' + endpoint, json=value)
            assert retry.status_code == 200, retry.text
            assert store.audio_vibes.samples(value['run_id']) == first
            assert annotations_by_kind() == expected
            if endpoint == 'samples':
                conflict = await client.post('/audio-vibes/samples', json={**value, 'title': 'Conflicting payload'})
                assert conflict.status_code == 409
            else:
                assert retry.json()['feedback']['vibes'] == ['cruise']

    asyncio.run(exercise())
    assert not app.state.ctx.work.active


def test_collection_acknowledgements_do_not_load_evidence_payloads(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    value = sample()
    app = create_app(store, lambda: {}, background_work=False)

    def forbidden(_run_id):
        raise AssertionError('A collection receipt must not load diagnostic sample payloads')

    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.post('/audio-vibes/samples', json=value).status_code == 200
        monkeypatch.setattr(store.audio_vibes, 'samples', forbidden)
        for response in (
            client.post('/audio-vibes/samples', json=value),
            client.post('/audio-vibes/runs', json={key: value[key] for key in ('run_id', 'video_id', 'title', 'artist')}),
            client.get('/audio-vibes/runs/' + value['run_id']),
        ):
            assert response.status_code == 200, response.text
            assert response.json()['samples'] == [{'id': value['id'], 'point': value['point']}]
