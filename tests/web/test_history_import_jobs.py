import asyncio
import json
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.bridge import Bridge
from yt_playlist.core import discovery_sources
from yt_playlist.library import spotify_data, takeout
from yt_playlist.web.history_imports import HistoryImports
from yt_playlist.web.routes import bridge, setup
from tests.test_spotify_library_import import _account_zip


REPORT = {'matched': 0, 'unmatched': 0, 'plays_added': 0, 'events_added': 0,
          'span_days': 0, 'unmatched_artists': {}, 'skipped': 0, 'unparsed_dates': 0}


def context(store):
    return SimpleNamespace(store=store, setup=None, rec_worker=None, now_fn=lambda: 1000,
                           work=Activity(), bridge=Bridge(),
                           templates=Jinja2Templates(directory='src/yt_playlist/web/templates'))


@pytest.fixture
def uploaded_files(monkeypatch):
    uploads = []
    initialize = UploadFile.__init__

    def track(upload, *args, **kwargs):
        initialize(upload, *args, **kwargs)
        uploads.append(upload)

    monkeypatch.setattr(UploadFile, '__init__', track)
    try:
        yield uploads
    finally:
        # A failed ownership assertion must not leak its diagnostic handles.
        for upload in uploads:
            upload.file.close()


@pytest.mark.parametrize('brand', [None, 'brand-account'])
def test_spotify_captured_identity_reaches_discovery_after_main_changes(store, monkeypatch, brand):
    first = store.accounts.upsert_account('First', 'one', None, True)
    second = store.accounts.upsert_account('Second', 'two', brand, False)
    store.tracks.upsert_track('known-video', 'Known', 'Known Artist', None, 180)
    raw = json.dumps([{
        'ts': f'2024-01-{day:02}T10:00:00Z', 'ms_played': 180000,
        'master_metadata_track_name': 'Known' if day == 1 else 'Unknown',
        'master_metadata_album_artist_name': 'Known Artist' if day == 1 else 'Unmatched Artist',
        'spotify_track_uri': 'spotify:track:' + ('known' if day == 1 else 'unknown'),
    } for day in range(1, 5)])
    imports = HistoryImports(context(store))
    entered, release = threading.Event(), threading.Event()
    parse = spotify_data.prepare_streaming_history

    def held_parse(payload):
        entered.set()
        assert release.wait(5), 'The test did not release parsing'
        return parse(payload)

    with monkeypatch.context() as held:
        held.setattr(spotify_data, 'prepare_streaming_history', held_parse)
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(lambda: asyncio.run(imports.run('spotify', raw)))
            try:
                assert entered.wait(2)
                store.accounts.upsert_account('First', 'one', None, False)
                store.accounts.upsert_account('Second', 'two', brand, True)
            finally:
                release.set()
            result = running.result(timeout=5)
    assert result['report']['identity_id'] == first
    assert result['report']['listens_added'] == 4 and result['seeded'] == 1
    assert {row[0] for row in store.conn.execute('SELECT account_id FROM spotify_history_listens')} == {first}

    def origins():
        return {row[0] for row in store.conn.execute(
            'SELECT o.account_id FROM discovery_artist_sources s JOIN discovery_origins o ON o.id=s.origin_id '
            "WHERE o.kind='spotify_export'")}

    assert origins() == {first}
    unrelated = asyncio.run(imports.run('spotify', raw))
    assert unrelated['report']['identity_id'] == second
    assert unrelated['report']['listens_added'] == 4
    assert origins() == {first, second}
    again = asyncio.run(imports.run('spotify', raw))
    assert again['report']['listens_added'] == 0
    assert store.conn.execute('SELECT count(*) FROM spotify_history_listens').fetchone()[0] == 8
    with store.conn:
        discovery_sources.remove_spotify_artists(store.conn, account_id=first)
    assert origins() == {second}
    assert len(store.discovery.get_discovered_artists()) == 1
    assert imports.ctx.work.wait(1)


def test_slow_import_keeps_requests_and_bridge_replies_responsive_and_serializes_history(
        store, monkeypatch, uploaded_files):
    store.accounts.upsert_account('main', 'credential', None, True)
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
                assert len(uploaded_files) == 1 and uploaded_files[0].file.closed
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
                assert len(uploaded_files) == 2 and all(upload.file.closed for upload in uploaded_files)
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
def test_import_error_is_terminal_visible_and_keeps_retry_form(store, monkeypatch, uploaded_files, source):
    def fail(*args, **kwargs):
        raise ValueError('bad <export>')

    module, function = ((takeout, 'import_takeout') if source == 'takeout' else
                        (spotify_data, 'import_spotify_data'))
    monkeypatch.setattr(module, function, fail)
    ctx = context(store)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    with TestClient(app) as client:
        response = client.post('/import/' + source, files=[
            ('file', ('history.json', b'data')), ('unused', ('extra.json', b'extra')),
        ])
        assert 'History import failed' in response.text
        assert 'HX-Retarget' not in response.headers
        status = client.get('/import/history/status?source=' + source,
                            headers={'accept': 'application/json'}).json()
        assert status['done'] and status['outcome'] == 'failed'
        assert status['error'] == 'bad <export>'
        assert 'bad &lt;export&gt;' in client.get('/import/history/status?source=' + source).text
        assert not ctx.jobs.has_active() and not ctx.work.active
        assert len(uploaded_files) == 2 and all(upload.file.closed for upload in uploaded_files)


@asynccontextmanager
async def held_history_import(imports, monkeypatch, report=REPORT):
    """Own the worker through failed assertions, with diagnostics before releasing it."""
    ctx = imports.ctx
    entered, release = threading.Event(), threading.Event()
    settings_entered = threading.Event()
    completed = Future()
    operations, worker = [], {}
    run = imports._run

    def diagnostics():
        frame = sys._current_frames().get(worker.get('ident'))
        return json.dumps({
            'active': ctx.work.active, 'status': imports.status('takeout'),
            'store_lock': repr(ctx.store._lock._lock), 'worker': worker,
            'operations': operations,
            'stack': traceback.format_stack(frame) if frame else [],
        }, indent=2)

    def measured(function, operation):
        @wraps(function)
        def call(*args, **kwargs):
            wall, cpu = time.monotonic(), time.thread_time()
            worker['operation'] = operation
            if operation == 'post-import settings':
                settings_entered.set()
            try:
                return function(*args, **kwargs)
            finally:
                operations.append({'operation': operation, 'wall': time.monotonic() - wall,
                                   'cpu': time.thread_time() - cpu})
                worker['operation'] = 'finishing import'
        return call

    @wraps(run)
    def observed_run(*args, **kwargs):
        worker.update(ident=threading.get_ident(), name=threading.current_thread().name,
                      operation='importing')
        try:
            return run(*args, **kwargs)
        finally:
            completed.set_result(None)

    def slow_import(*args, **kwargs):
        entered.set()
        assert release.wait(30), 'The test did not release its importer'
        return report.copy()

    monkeypatch.setattr(takeout, 'import_takeout', slow_import)
    monkeypatch.setattr(imports, '_run', observed_run)
    monkeypatch.setattr(ctx.store.settings, 'set_setting',
                        measured(ctx.store.settings.set_setting, 'post-import settings'))
    monkeypatch.setattr(takeout, 'seed_discovery_from_unmatched',
                        measured(takeout.seed_discovery_from_unmatched, 'post-import discovery'))
    task = asyncio.create_task(imports.run('takeout', b'data'))
    try:
        yield SimpleNamespace(task=task, entered=entered, release=release, completed=completed,
                              settings_entered=settings_entered,
                              operations=operations, diagnostics=diagnostics)
    except BaseException as exc:
        exc.add_note(diagnostics())
        raise
    finally:
        ctx.work.close_admission()
        release.set()
        assert await asyncio.to_thread(ctx.work.wait, 30), diagnostics()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def cancel_held_import(imports, held):
    assert await asyncio.to_thread(held.entered.wait, 2), held.diagnostics()
    held.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await held.task
    imports.ctx.work.close_admission()
    assert imports.ctx.work.active
    assert not imports.status('takeout')['done']
    with pytest.raises(WorkClosing):
        await imports.run('spotify', b'refused')
    assert imports.status('spotify')['outcome'] == 'failed'


async def finish_held_import(imports, held):
    held.release.set()
    # Real settings/discovery work has its own bounded completion phase. The
    # two-second check then tests Activity releasing ownership, not disk speed.
    await asyncio.to_thread(held.completed.result, 30)
    assert await asyncio.to_thread(imports.ctx.work.wait, 2), held.diagnostics()
    status = imports.status('takeout')
    assert status['done'] and status['outcome'] == 'complete', held.diagnostics()


def test_cancelled_import_request_remains_owned_until_worker_finishes(store, monkeypatch):
    store.accounts.upsert_account('main', 'credential', None, True)
    imports = HistoryImports(context(store))

    async def run():
        async with held_history_import(imports, monkeypatch) as held:
            await cancel_held_import(imports, held)
            await finish_held_import(imports, held)
            assert [op['operation'] for op in held.operations] == [
                'post-import settings', 'post-import discovery']
            assert store.settings.get_setting('takeout_nag_dismissed_at') == '1000'

    asyncio.run(run())


def test_cancelled_import_failure_cleanup_drains_before_store_closes(store, monkeypatch):
    store.accounts.upsert_account('main', 'credential', None, True)
    imports = HistoryImports(context(store))

    async def run():
        async with held_history_import(imports, monkeypatch) as held:
            await cancel_held_import(imports, held)
            raise AssertionError('controlled ownership assertion failure')

    with pytest.raises(AssertionError, match='controlled ownership assertion failure') as failed:
        asyncio.run(run())
    snapshot = json.loads(failed.value.__notes__[0])
    assert snapshot['active'] == ['_run'] and not snapshot['status']['done']
    assert snapshot['worker']['operation'] == 'importing'
    assert any('slow_import' in line for line in snapshot['stack'])
    assert not imports.ctx.work.active
    assert imports.status('takeout')['outcome'] == 'complete'
    assert store.settings.get_setting('takeout_nag_dismissed_at') == '1000'


def test_cancelled_import_stays_owned_during_real_post_import_lock_wait(store, monkeypatch):
    store.accounts.upsert_account('main', 'credential', None, True)
    imports = HistoryImports(context(store))
    report = {**REPORT, 'unmatched': 3, 'unmatched_artists': {'New Artist': 3}}

    async def run():
        async with held_history_import(imports, monkeypatch, report) as held:
            await cancel_held_import(imports, held)
            with store._lock:
                held.release.set()
                assert await asyncio.to_thread(held.settings_entered.wait, 5)
                assert not await asyncio.to_thread(imports.ctx.work.wait, 2)
                snapshot = json.loads(held.diagnostics())
                assert snapshot['active'] == ['_run'] and not snapshot['status']['done']
                assert snapshot['worker']['operation'] == 'post-import settings'
                assert f'owner={threading.get_ident()}' in snapshot['store_lock']
                assert any('acquire' in line for line in snapshot['stack'])
            await finish_held_import(imports, held)
            assert held.operations[0]['wall'] >= 2

    asyncio.run(run())
    assert store.settings.get_setting('takeout_nag_dismissed_at') == '1000'
    assert {row['artist'] for row in store.discovery.get_discovered_artists()} == {'New Artist'}


def test_library_decode_keeps_requests_and_bridge_responsive_and_is_drained(
        store, monkeypatch, uploaded_files):
    ctx = context(store)
    ctx.spotify_import_lock = threading.Lock()
    store.accounts.upsert_account('main', 'credential', None, True)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    app.include_router(bridge.build(ctx))
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    decode = setup.load_spotify_library

    @app.get('/responsive')
    async def responsive():
        return {'thread': threading.get_ident()}

    def slow_decode(raw):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return decode(raw)

    monkeypatch.setattr(setup, 'load_spotify_library', slow_decode)
    with ThreadPoolExecutor(max_workers=4) as callers, TestClient(app) as client:
        with client.websocket_connect('/bridge/ws', headers={'origin': bridge.EXTENSION_ORIGIN}) as ws:
            upload = callers.submit(client.post, '/import/spotify-library/v6',
                                    files={'file': ('Account Data.zip', _account_zip())})
            try:
                assert entered.wait(2)
                assert len(uploaded_files) == 1 and uploaded_files[0].file.closed
                ping = callers.submit(client.get, '/responsive').result(timeout=1).json()
                assert ping['thread'] != worker_threads[0]
                remote = callers.submit(ctx.bridge.execute, 'GET', 'test', None, timeout=2)
                frame = callers.submit(ws.receive_json).result(timeout=1)
                ws.send_json({'id': frame['id'], 'status': 200, 'body': 'reply'})
                assert remote.result(timeout=1) == (200, 'reply')
                assert not upload.done()
                ctx.work.close_admission()
                assert not ctx.work.wait(.01)
            finally:
                release.set()
            response = upload.result(timeout=2)
            assert response.status_code == 200
            assert 'Road songs' in response.text and 'selection_token' in response.text
            assert response.headers['HX-Retarget'] == '#spotify-library-import-block'
            assert ctx.work.wait(2)


def test_cancelled_library_upload_keeps_decoder_owned(store, monkeypatch, uploaded_files):
    ctx = context(store)
    ctx.spotify_import_lock = threading.Lock()
    store.accounts.upsert_account('main', 'credential', None, True)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    entered, release = threading.Event(), threading.Event()
    decode = setup.load_spotify_library

    def slow_decode(raw):
        entered.set()
        assert release.wait(5)
        return decode(raw)

    monkeypatch.setattr(setup, 'load_spotify_library', slow_decode)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            upload = asyncio.create_task(client.post('/import/spotify-library',
                files={'file': ('Account Data.zip', _account_zip())}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert len(uploaded_files) == 1 and uploaded_files[0].file.closed
                upload.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await upload
                assert uploaded_files[0].file.closed
                ctx.work.close_admission()
                assert ctx.work.active and not ctx.work.wait(.01)
            finally:
                release.set()
            assert await asyncio.to_thread(ctx.work.wait, 2)

    asyncio.run(exercise())


def test_library_upload_format_error_keeps_guidance_and_releases_work(store, uploaded_files):
    ctx = context(store)
    ctx.spotify_import_lock = threading.Lock()
    store.accounts.upsert_account('main', 'credential', None, True)
    app = FastAPI()
    app.include_router(setup.build(ctx))
    with TestClient(app) as client:
        response = client.post('/import/spotify-library/v6', files={'file': ('bad.zip', b'invalid')})
    assert response.status_code == 200
    assert 'Could not find Spotify playlists or saved albums' in response.text
    assert 'Account Data' in response.text and 'selection_token' not in response.text
    assert not ctx.work.active
    assert len(uploaded_files) == 1 and uploaded_files[0].file.closed


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
