"""A slow Vibe reader must leave the application able to serve other requests."""
import asyncio
from contextlib import contextmanager
import threading
import time

import httpx
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from tests.home_fixture_support import returning_listener
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.library import listening_window
from yt_playlist.rec import account_growth, home_vibe, mix_tuning
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home_views import HomeViews


def render_preset(self, request, now, *, remaining):
    preset = home_vibe.current(self.store, now)
    return self.tuning_response(Response(preset), now, remaining=remaining, active=bool(preset))


def test_earned_vibe_selection_reads_access_once_and_skips_on_air(file_backed_store, monkeypatch):
    store = file_backed_store
    returning_listener(store, 'vibes', now=1000)
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '900')
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    ctx = app.state.ctx
    monkeypatch.setattr(HomeViews, 'cards_safe', render_preset)
    reads, sessions, expirations = [], [], []
    choice_usage, active_session = RecommendationProposals.choice_usage, listening_window.active_session
    expire = mix_tuning.expire

    def usage(repo):
        reads.append(repo)
        return choice_usage(repo)

    def session(source, now):
        sessions.append(source)
        return active_session(source, now)

    def expiration(source, now, **kwargs):
        expirations.append(source._lock.held_by_current_thread)
        return expire(source, now, **kwargs)

    def unexpected_bootstrap(**kwargs):
        pytest.fail('A previously earned Vibe selection loaded On Air')

    with TestClient(app, base_url='http://127.0.0.1') as client:
        monkeypatch.setattr(ctx.radio, 'bootstrap', unexpected_bootstrap)
        monkeypatch.setattr(RecommendationProposals, 'choice_usage', usage)
        monkeypatch.setattr(listening_window, 'active_session', session)
        monkeypatch.setattr(mix_tuning, 'expire', expiration)
        response = client.post('/home/vibe', data={'vibe': 'relax'})

    assert response.status_code == 200
    assert response.headers['X-Mix-Tuning-Active'] == 'true'
    assert float(response.headers['X-Mix-Tuning-Remaining']) == mix_tuning.TTL_S - 100
    assert len(reads) == 1
    assert expirations == [True]
    assert sessions and all(source is store for source in sessions)
    assert response.text == 'relax'
    assert 'X-Home-Vibe-Request' not in response.headers


@pytest.mark.parametrize('allowed,status', [(True, 400), (False, 409)], ids=['invalid', 'locked'])
def test_rejected_vibe_does_not_wait_for_an_unrelated_writer(file_backed_store, monkeypatch, allowed, status):
    store = file_backed_store
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *args, **kwargs: allowed)
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


@pytest.mark.parametrize('phase', ['admission', 'cleanup', 'assembly'])
def test_vibe_reader_waits_leave_the_event_loop_responsive(file_backed_store, monkeypatch, phase):
    store = file_backed_store
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    # This fault probe isolates reader ownership, independently of music readiness.
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *args, **kwargs: True)
    entered, release = threading.Event(), threading.Event()
    original_view = store.read_view

    def wait():
        entered.set()
        assert release.wait(5), 'The test did not release the private reader'

    @contextmanager
    def delayed_view(**kwargs):
        if phase == 'admission':
            wait()
        try:
            with original_view(**kwargs) as reader:
                yield reader
        finally:
            if phase == 'cleanup':
                wait()

    def delayed_render(self, request, now, *, remaining):
        if phase == 'assembly':
            wait()
        return render_preset(self, request, now, remaining=remaining)

    monkeypatch.setattr(store, 'read_view', delayed_view)
    monkeypatch.setattr(HomeViews, 'cards_safe', delayed_render)

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
            assert response.status_code == 200 and response.text == 'concentrate'
            assert store.settings.get_setting('home_mix_vibe') == 'concentrate'
            # Cleanup releases the context and admission for the next manual choice.
            response = await client.post('/home/vibe', data={'vibe': 'relax'})
            assert response.status_code == 200 and response.text == 'relax'
            assert store.settings.get_setting('home_mix_vibe') == 'relax'

    asyncio.run(exercise())
    assert not app.state.ctx.work.active
