import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from yt_playlist.core.activity import Activity
from yt_playlist.core.bridge import Bridge
from yt_playlist.library import spotify_data, takeout
from yt_playlist.web.history_imports import HistoryImports
from yt_playlist.web.routes import bridge, setup


REPORT = {'matched': 0, 'unmatched': 0, 'plays_added': 0, 'events_added': 0,
          'span_days': 0, 'unmatched_artists': {}, 'skipped': 0, 'unparsed_dates': 0}


def context(store):
    return SimpleNamespace(store=store, setup=None, rec_worker=None, now_fn=lambda: 1000,
                           work=Activity(), bridge=Bridge(),
                           templates=Jinja2Templates(directory='src/yt_playlist/web/templates'))


def test_slow_import_keeps_requests_and_bridge_replies_responsive_and_serializes_history(store, monkeypatch):
    ctx = context(store)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    app.include_router(bridge.build(ctx))
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    worker_threads = []

    @app.get('/responsive')
    async def responsive():
        return {'thread': threading.get_ident()}

    def slow_import(st, raw, *, on_progress):
        worker_threads.append(threading.get_ident())
        on_progress('Reading a slow export…')
        entered.set()
        assert release.wait(5)
        return REPORT.copy()

    def second_import(st, raw, *, on_progress):
        second_entered.set()
        return REPORT.copy()

    monkeypatch.setattr(takeout, 'import_takeout', slow_import)
    monkeypatch.setattr(spotify_data, 'import_spotify_data', second_import)
    with ThreadPoolExecutor(max_workers=5) as callers, TestClient(app) as client:
        with client.websocket_connect('/bridge/ws', headers={'origin': bridge.EXTENSION_ORIGIN}) as ws:
            first = callers.submit(client.post, '/import/takeout', files={'file': ('history.json', b'first')})
            try:
                assert entered.wait(2)
                # Both requests use the same running ASGI loop as the blocked import request.
                ping = callers.submit(client.get, '/responsive').result(timeout=2).json()
                assert ping['thread'] != worker_threads[0]
                status = callers.submit(client.get, '/import/history/status?source=takeout',
                                        headers={'accept': 'application/json'}).result(timeout=2).json()
                assert not status['done'] and status['message'] == 'Reading a slow export…'
                remote = callers.submit(ctx.bridge.execute, 'GET', 'test', None, timeout=2)
                frame = callers.submit(ws.receive_json).result(timeout=2)
                ws.send_json({'id': frame['id'], 'status': 200, 'body': 'reply'})
                assert remote.result(timeout=2) == (200, 'reply')
                second = callers.submit(client.post, '/import/spotify', files={'file': ('history.json', b'second')})
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    waiting = client.get('/import/history/status?source=spotify',
                                         headers={'accept': 'application/json'}).json()
                    if waiting['job_id'] is not None:
                        break
                    time.sleep(.01)
                assert waiting['job_id'] is not None and not waiting['done']
                assert not second_entered.is_set()
                # Both running and queued imports belong to the application drain.
                ctx.work.close_admission()
                assert not ctx.work.wait(.01)
            finally:
                release.set()
            assert 'Import complete' in first.result(timeout=3).text
            assert 'Spotify import complete' in second.result(timeout=3).text
            assert second_entered.is_set()
            assert ctx.work.wait(2)
            completed = client.get('/import/history/status?source=takeout',
                                   headers={'accept': 'application/json'}).json()
            assert completed['done'] and completed['outcome'] == 'complete'


@pytest.mark.parametrize('source', ['takeout', 'spotify'])
def test_import_error_is_terminal_visible_and_keeps_retry_form(store, monkeypatch, source):
    def fail(*args, **kwargs):
        raise ValueError('bad <export>')

    module, function = ((takeout, 'import_takeout') if source == 'takeout' else
                        (spotify_data, 'import_spotify_data'))
    monkeypatch.setattr(module, function, fail)
    ctx = context(store)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    with TestClient(app) as client:
        response = client.post('/import/' + source, files={'file': ('history.json', b'data')})
        assert 'History import failed' in response.text
        assert 'HX-Retarget' not in response.headers
        status = client.get('/import/history/status?source=' + source,
                            headers={'accept': 'application/json'}).json()
        assert status['done'] and status['outcome'] == 'failed'
        assert status['error'] == 'bad <export>'
        assert 'bad &lt;export&gt;' in client.get('/import/history/status?source=' + source).text
        assert not ctx.jobs.has_active() and not ctx.work.active


def test_cancelled_import_request_remains_owned_until_worker_finishes(store, monkeypatch):
    ctx = context(store)
    imports = HistoryImports(ctx)
    entered, release = threading.Event(), threading.Event()

    def slow_import(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return REPORT.copy()

    monkeypatch.setattr(takeout, 'import_takeout', slow_import)

    async def run():
        task = asyncio.create_task(imports.run('takeout', b'data'))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            ctx.work.close_admission()
            assert ctx.work.active
            assert not imports.status('takeout')['done']
        finally:
            release.set()
        assert await asyncio.to_thread(ctx.work.wait, 2)
        assert imports.status('takeout')['outcome'] == 'complete'

    asyncio.run(run())


def test_removal_waits_for_model_publication_without_blocking_requests(store):
    ctx = context(store)
    model_lock = threading.Lock()
    triggered = threading.Event()
    ctx.rec_worker = SimpleNamespace(model_lock=model_lock,trigger=triggered.set)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    @app.get('/responsive')
    async def responsive():
        return {'ok':True}
    with ThreadPoolExecutor(max_workers=2) as callers, TestClient(app) as client:
        model_lock.acquire()
        try:
            removal = callers.submit(client.post,'/import/spotify/remove')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = client.get('/import/history/status?source=spotify',
                                    headers={'accept':'application/json'}).json()
                if status['job_id'] is not None:
                    break
                time.sleep(.01)
            assert status['job_id'] is not None and not status['done']
            assert not removal.done() and not triggered.is_set()
            assert client.get('/responsive').json() == {'ok':True}
            assert ctx.work.active
        finally:
            model_lock.release()
        assert 'Spotify history removed' in removal.result(timeout=3).text
        assert triggered.is_set() and ctx.work.wait(2)
