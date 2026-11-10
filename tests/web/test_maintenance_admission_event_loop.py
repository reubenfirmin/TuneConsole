"""A busy durable-feedback inbox must not stall unrelated foreground requests."""
import asyncio
from contextlib import closing
import threading
import time

import httpx
import pytest

from yt_playlist.core import playback_feedback
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('maintenance_active', [False, True])
def test_state_change_waits_for_maintenance_off_event_loop(file_backed_store, maintenance_active):
    store = file_backed_store
    inbox = playback_feedback.inbox(store)
    with closing(inbox.connect()):
        pass
    app = create_app(store, lambda: {}, background_work=False)
    app.state.ctx.storage_maintenance.active = maintenance_active
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
            pending = asyncio.create_task(client.post('/_test/state-change'))
            started = time.monotonic()
            try:
                await asyncio.sleep(.05)
                for path in ('/_test/event-loop', '/bridge/status'):
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
                assert not expired.is_set(), 'Feedback admission blocked the event loop until its safety gate expired'
                assert time.monotonic() - started < 1
                assert not changed, 'The state change must await the maintenance decision'
                assert not pending.done()
            finally:
                release.set()
                result = await asyncio.wait_for(pending, 5)
            assert result.status_code == (503 if maintenance_active else 200)
            assert bool(changed) is not maintenance_active

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        holder.join(3)
        assert not holder.is_alive()
    assert not app.state.ctx.work.active
