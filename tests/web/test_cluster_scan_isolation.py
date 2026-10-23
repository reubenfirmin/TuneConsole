"""Clusters scans keep ASGI/bridge work live and own their private readers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import numpy as np
import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.bridge import Bridge
from yt_playlist.core.store import Store
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.web.routes import bridge, clusters


@pytest.fixture(params=['memory', 'file'])
def database(request, tmp_path):
    store = Store(':memory:' if request.param == 'memory' else tmp_path / 'clusters.db')
    store.init_schema()
    store.identities.upsert_identity('Listener', 'fixture', None, True)
    keys = []
    for i in range(3):
        store.tracks.upsert_track(f'v{i}', f'Track {i}', f'Artist {i}', '', 200)
        keys.append(store.tracks.identity_key_for_video(f'v{i}'))
    store.rec_model.replace_rec_vectors([(key, np.asarray([1, i / 10], np.float32).tobytes())
                                         for i, key in enumerate(keys)])
    yield store, keys
    store.conn.close()


def application(store):
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1000, work=Activity(), bridge=Bridge(), templates=None)
    app = FastAPI()
    app.include_router(clusters.build(ctx))
    app.include_router(bridge.build(ctx))

    @app.get('/responsive')
    async def responsive():
        # A synchronous serving read/write must also avoid a private scan's lock.
        store.settings.set_setting('ui-write', 'responsive')
        return {'thread': threading.get_ident(), 'value': store.settings.get_setting('ui-write')}

    return ctx, app


def slow_vector_loading(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    readers, threads = [], []
    original = RecModelRepo.get_rec_vectors

    def load(repo):
        assert repo.conn is not store.conn
        readers.append(repo.conn)
        threads.append(threading.get_ident())
        repo.conn.create_function('scan_started', 0, lambda: (entered.set(), 0)[1])
        repo.conn.create_function('scan_released', 0, lambda: int(release.is_set()))
        # Slow the actual vector-loading SQL boundary, then load/score real saved
        # vectors. Cancellation uses the Store's normal SQLite progress handler.
        repo.conn.execute('WITH RECURSIVE slow(n) AS (VALUES(scan_started()) UNION ALL '
                          'SELECT n+1 FROM slow WHERE n<1000000000 AND scan_released()=0) '
                          'SELECT sum(n) FROM slow').fetchone()
        return original(repo)

    monkeypatch.setattr(RecModelRepo, 'get_rec_vectors', load)
    return entered, release, readers, threads


@pytest.mark.parametrize('surface', ['expand', 'explain'])
def test_slow_scan_keeps_http_and_bridge_live_and_serializes_other_scans(database, monkeypatch, surface):
    store, keys = database
    ctx, app = application(store)
    entered, release, readers, threads = slow_vector_loading(store, monkeypatch)
    body = {'pos_keys': [keys[0]], 'key': keys[1], 'path_keys': [keys[0]]}
    with ThreadPoolExecutor(5) as callers, TestClient(app) as client:
        with client.websocket_connect('/bridge/ws', headers={'origin': bridge.EXTENSION_ORIGIN}) as ws:
            first = callers.submit(client.post, '/clusters/' + surface, json=body)
            try:
                assert entered.wait(2)
                ping = callers.submit(client.get, '/responsive').result(2).json()
                assert ping['thread'] != threads[0] and ping['value'] == 'responsive'
                remote = callers.submit(ctx.bridge.execute, 'GET', 'test', None, timeout=2)
                frame = callers.submit(ws.receive_json).result(2)
                ws.send_json({'id': frame['id'], 'status': 200, 'body': 'reply'})
                assert remote.result(2) == (200, 'reply')
                second = callers.submit(client.post, '/clusters/explain', json=body)
                # A same-loop ping runs after the second request has been submitted.
                assert callers.submit(client.get, '/responsive').result(2).status_code == 200
                assert len(readers) == 1
                assert not ctx.work.wait(.01)
            finally:
                release.set()
            assert first.result(3).status_code == 200
            assert second.result(3).status_code == 200
            # Expansion's existing measurement path may read additional vectors
            # after scoring; those readers must also be private and released.
            assert len(readers) >= 2
            assert ctx.work.wait(2)
            for reader in readers:
                with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                    reader.execute('SELECT 1')


def test_cancelled_http_scan_interrupts_sql_and_does_not_publish(database, monkeypatch):
    store, keys = database
    ctx, app = application(store)
    entered, release, readers, _ = slow_vector_loading(store, monkeypatch)
    offered = []
    monkeypatch.setattr(clusters.measurement, 'record_recommendations', lambda *a, **kw: offered.append(True))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            task = asyncio.create_task(client.post('/clusters/expand', json={'pos_keys': [keys[0]]}))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(ctx.work.wait, 2)
            assert not offered
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                readers[0].execute('SELECT 1')
            release.set()
            # The cancelled worker releases the slot only after it finishes.
            result = await asyncio.wait_for(client.post('/clusters/expand', json={'pos_keys': [keys[0]]}), 3)
            assert result.status_code == 200 and result.json()['ring']

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        assert ctx.work.wait(3)


def test_shutdown_interrupts_active_scan_and_releases_reader(database, monkeypatch):
    store, keys = database
    ctx, app = application(store)
    entered, release, readers, _ = slow_vector_loading(store, monkeypatch)
    with ThreadPoolExecutor(1) as callers, TestClient(app) as client:
        pending = callers.submit(client.post, '/clusters/explain',
                                 json={'key': keys[1], 'path_keys': [keys[0]]})
        try:
            assert entered.wait(2)
            ctx.work.close_admission()
            assert ctx.work.wait(2)
            with pytest.raises(WorkClosing):
                pending.result(2)
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                readers[0].execute('SELECT 1')
        finally:
            release.set()
