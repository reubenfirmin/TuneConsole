"""A busy durable-feedback inbox must not stall unrelated foreground requests."""
import asyncio
import threading

import httpx
import pytest

from yt_playlist.core import playback_feedback
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('maintenance_active', [False, True])
def test_requests_do_not_wait_for_feedback_inbox(file_backed_store, maintenance_active):
    store = file_backed_store
    inbox = playback_feedback.inbox(store).file
    app = create_app(store, lambda: {}, background_work=False)
    file_backed_store.activity_paused = maintenance_active
    entered, release, expired = threading.Event(), threading.Event(), threading.Event()
    changed = []

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    @app.post('/_test/state-change')
    def change():
        changed.append(True)
        return {'changed': True}

    def hold_feedback_admission():
        with inbox.admission():
            entered.set()
            # Release independently of the event loop even if the regression
            # blocks it. This is a real inbox lock, not a sleeping HTTP handler.
            if not release.wait(2):
                expired.set()

    holder = threading.Thread(target=hold_feedback_admission)
    holder.start()
    assert entered.wait(1)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            try:
                result = await asyncio.wait_for(client.post('/_test/state-change'), 1)
                for path in ('/_test/event-loop', '/bridge/status'):
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
                assert not expired.is_set(), 'Feedback admission blocked the event loop until its safety gate expired'
            finally:
                release.set()
            assert result.status_code == (503 if maintenance_active else 200)
            assert bool(changed) is not maintenance_active

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        holder.join(3)
        assert not holder.is_alive()
    assert not app.state.ctx.work.active
