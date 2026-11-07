"""A slow Vibe reader must leave the application able to serve other requests."""
import asyncio
from contextlib import contextmanager
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import account_growth
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('allowed,status', [(True, 400), (False, 409)], ids=['invalid', 'locked'])
def test_rejected_vibe_does_not_wait_for_an_unrelated_writer(file_backed_store, monkeypatch, allowed, status):
    store = file_backed_store
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    monkeypatch.setattr(account_growth, 'retained_access', lambda *_: True)
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *_: allowed)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with store._lock:
            entered.set()
            release.wait(5)

    with TestClient(app, base_url='http://127.0.0.1') as client:
        worker = threading.Thread(target=hold)
        worker.start()
        watchdog = threading.Timer(3, release.set)
        watchdog.start()
        try:
            assert entered.wait(1)
            started = time.monotonic()
            response = client.post('/home/vibe', data={'vibe': 'invalid-preset'})
            assert response.status_code == status
            assert time.monotonic() - started < 1, 'A rejected Vibe waited for the writer'
        finally:
            release.set()
            watchdog.cancel()
            watchdog.join(1)
            worker.join(1)
        assert not worker.is_alive()
    assert store.settings.get_setting('home_mix_vibe') is None


@pytest.mark.parametrize('phase', ['admission', 'cleanup'])
def test_vibe_reader_waits_leave_the_event_loop_responsive(file_backed_store, monkeypatch, phase):
    store = file_backed_store
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    # This fault probe isolates reader ownership, independently of music readiness.
    monkeypatch.setattr(account_growth, 'retained_access', lambda *_: True)
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *_: True)
    # This request accepts durable intent. Generation has separate worker owners.
    monkeypatch.setattr(app.state.ctx.vibe_requests, 'resume', lambda: None)
    entered, release = threading.Event(), threading.Event()
    original_view = store.read_view

    def wait():
        entered.set()
        assert release.wait(5), 'The test did not release the private reader'

    @contextmanager
    def delayed_view(*, tracked=False):
        if phase == 'admission':
            wait()
        try:
            with original_view(tracked=tracked) as reader:
                yield reader
        finally:
            if phase == 'cleanup':
                wait()

    monkeypatch.setattr(store, 'read_view', delayed_view)

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            # An independent watchdog also releases a broken event loop so this
            # regression fails promptly instead of hanging the test process.
            watchdog = threading.Timer(3, release.set)
            watchdog.start()
            started = time.monotonic()
            selection = asyncio.create_task(client.post('/home/vibe', data={'vibe': 'concentrate'}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                response = await asyncio.wait_for(client.get('/_test/event-loop'), 1)
                assert response.status_code == 200 and response.json() == {'ok': True}
                assert time.monotonic() - started < 1, 'Vibe reader blocked the event loop'
                assert not selection.done(), 'The probe did not overlap the blocked reader'
            finally:
                release.set()
                watchdog.cancel()
                watchdog.join(1)
                response = await asyncio.wait_for(selection, 5)
            assert response.status_code == 200 and response.text == ''
            assert response.headers['HX-Reswap'] == 'none'
            accepted = app.state.ctx.vibe_requests.state()
            assert accepted == {'id': response.headers['X-Home-Vibe-Request'],
                                'vibe': 'concentrate', 'phase': 'pending'}
            assert store.settings.get_setting('home_mix_vibe') == 'concentrate'
            # Cleanup releases the context and admission for the next manual choice.
            response = await client.post('/home/vibe', data={'vibe': 'relax'})
            assert response.status_code == 200
            assert response.headers['HX-Reswap'] == 'none'
            latest = app.state.ctx.vibe_requests.state()
            assert latest == {'id': response.headers['X-Home-Vibe-Request'],
                              'vibe': 'relax', 'phase': 'pending'}
            assert latest['id'] != accepted['id']
            assert store.settings.get_setting('home_mix_vibe') == 'relax'

    asyncio.run(exercise())
    assert not app.state.ctx.work.active
