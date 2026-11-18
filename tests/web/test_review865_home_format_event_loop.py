"""Independent P0 regression: a Home section change must not stall the event loop."""
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from yt_playlist.web.app import create_app
from yt_playlist.web.routes import home_discovery


READINESS_GATE_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 10


@pytest.mark.parametrize('case', ['worker-control', 'directions', 'radio'])
def test_home_section_readiness_keeps_unrelated_requests_responsive(store, monkeypatch, case, request):
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey

    early_journey(store)
    publish_personal_mix(store)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    original = home_discovery.growth_state
    calls = []
    root = Path(__file__).resolve().parents[2]
    sources = [root / name for name in (
        'src/yt_playlist/web/routes/home_discovery.py',
        'src/yt_playlist/web/routes/home_access.py',
        'src/yt_playlist/web/app.py',
    )]
    before = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sources}

    facts = {'case': case, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
             'background_work': False, 'controlled_readiness_gate_seconds': READINESS_GATE_SECONDS,
             'request_timeout_seconds': REQUEST_TIMEOUT_SECONDS, 'entered': False,
             'source_before': before, 'calls': calls}

    async def exercise():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = loop.create_future()
        release, expired = threading.Event(), threading.Event()

        def record_entry(entered_at):
            if not entered.done():
                entered.set_result(entered_at)

        def held_readiness(*args, **kwargs):
            thread = threading.get_ident()
            calls.append({'thread': thread, 'on_event_loop': thread == loop_thread})
            loop.call_soon_threadsafe(record_entry, time.monotonic())
            # The gate starts at readiness entry and opens even if this call stalls the loop.
            if not release.wait(READINESS_GATE_SECONDS):
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

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            url = '/_review865/worker-control' if case == 'worker-control' else '/home/format'
            started = time.monotonic()
            pending = asyncio.create_task(client.post(url, data={'panel': case}))
            try:
                await asyncio.wait((entered, pending), timeout=REQUEST_TIMEOUT_SECONDS,
                                   return_when=asyncio.FIRST_COMPLETED)
                assert entered.done(), 'The handler did not reach readiness evaluation'
                entered_at = entered.result()
                facts.update(entered=True, admission_elapsed=entered_at - started)

                response = await asyncio.wait_for(client.get('/_review865/event-loop'),
                                                  REQUEST_TIMEOUT_SECONDS)
                facts.update(probe_status=response.status_code,
                             probe_elapsed=time.monotonic() - entered_at,
                             gate_expired_before_probe=expired.is_set(), request_finished=pending.done())
                assert response.status_code == 200
                assert not facts['gate_expired_before_probe'], (
                    'The probe did not finish before the readiness safety gate expired'
                )
                assert not facts['request_finished'], 'The probe must finish while readiness work is still held'
                assert all(not call['on_event_loop'] for call in calls), 'Readiness ran on the event loop'
            finally:
                release.set()
                result = await asyncio.wait_for(pending, REQUEST_TIMEOUT_SECONDS)
                facts['request_status'] = result.status_code
            assert result.status_code in (200, 204, 409)

    try:
        asyncio.run(exercise())
    finally:
        facts['source_after'] = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sources}
        xml_path = request.config.getoption('xmlpath')
        report_dir = Path(xml_path).parent if xml_path else root / 'build/test-reports'
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f'review865-home-format-{case}-{uuid4().hex}.json'
        report.write_text(json.dumps(facts, indent=2) + '\n')
        request.node.user_properties.append(('review865_evidence', str(report)))
