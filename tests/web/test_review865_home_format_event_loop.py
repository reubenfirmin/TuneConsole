"""Independent P0 regression: a Home section change must not stall the event loop."""
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import threading
import time

import httpx
import pytest
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from yt_playlist.web.app import create_app
from yt_playlist.web.routes import home_discovery


@pytest.mark.parametrize('case', ['worker-control', 'directions', 'radio'])
def test_home_section_readiness_keeps_unrelated_requests_responsive(store, monkeypatch, case, request):
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey

    early_journey(store)
    publish_personal_mix(store)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    original = home_discovery.growth_state
    entered, release, expired = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    calls = []
    root = Path(__file__).resolve().parents[2]
    sources = [root / name for name in (
        'src/yt_playlist/web/routes/home_discovery.py',
        'src/yt_playlist/web/routes/home_access.py',
        'src/yt_playlist/web/app.py',
    )]
    before = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sources}

    def held_readiness(*args, **kwargs):
        calls.append({'thread': threading.get_ident(), 'on_event_loop': threading.get_ident() == loop_thread})
        entered.set()
        # This bounded gate represents waiting for readiness/database work. A
        # regression releases itself so an event-loop stall cannot hang pytest.
        if not release.wait(2):
            expired.set()
            release.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(home_discovery, 'growth_state', held_readiness)

    @app.get('/_review865/event-loop')
    async def probe():
        return {'ok': True}

    @app.post('/_review865/worker-control')
    async def worker_control(incoming: Request):
        await run_in_threadpool(held_readiness, app.state.ctx, incoming)
        return {'ok': True}

    facts = {'case': case, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
             'background_work': False, 'controlled_readiness_gate_seconds': 2,
             'source_before': before, 'calls': calls}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            url = '/_review865/worker-control' if case == 'worker-control' else '/home/format'
            started = time.monotonic()
            pending = asyncio.create_task(client.post(url, data={'panel': case}))
            try:
                reached = await asyncio.to_thread(entered.wait, 1)
                response = await client.get('/_review865/event-loop')
                facts.update(entered=reached, probe_status=response.status_code,
                             probe_elapsed=time.monotonic() - started,
                             gate_expired_before_probe=expired.is_set(), request_finished=pending.done())
                assert reached, 'The real handler did not reach readiness evaluation'
                assert response.status_code == 200
                assert not expired.is_set(), 'Home readiness blocked the event loop until its safety gate expired'
                assert not pending.done(), 'The probe must finish while readiness work is still held'
                assert all(not call['on_event_loop'] for call in calls)
            finally:
                release.set()
                result = await asyncio.wait_for(pending, 10)
                facts['request_status'] = result.status_code
            assert result.status_code in (200, 204, 409)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        facts['source_after'] = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sources}
        report = root / 'build/test-reports' / f'review865-home-format-20261104-{case}.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(facts, indent=2) + '\n')
        request.node.user_properties.append(('review865_evidence', str(report)))
