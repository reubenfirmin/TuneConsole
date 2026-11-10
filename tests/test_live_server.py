"""Live HTTP work must finish before its Store and native evidence disappear."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from fastapi import APIRouter, BackgroundTasks, FastAPI
import pytest
import uvicorn

from tests.live_server import LiveServer
from tests.store_fixture_support import initialize_consumer, without_query_timing
from yt_playlist.core import evidence_ledger
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence
from yt_playlist.web.app import create_app
from yt_playlist.web import app as webapp


@pytest.mark.parametrize('ending', ['normal', 'failure', 'interrupt'])
@pytest.mark.parametrize('work_kind', ['request', 'background'])
@pytest.mark.usefixtures('without_query_timing')
def test_teardown_drains_real_response_before_removing_its_store(
        tmp_path, _pristine_current_schema, monkeypatch, caplog, ending, work_kind, request):
    held, release, owner_finished = threading.Event(), threading.Event(), threading.Event()
    draining = threading.Event()
    observations = {}
    original_sync = evidence_ledger._sync_directory
    # The real lifespan, request admission and native writer remain in use.
    # Register only this test's endpoint, rather than every application route.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)

    with ThreadPoolExecutor(max_workers=2) as clients:
        with tempfile.TemporaryDirectory(dir=tmp_path, prefix='request-owner-') as directory:
            directory = Path(directory)
            store = Store(directory / 'home.db')
            try:
                # This owner retains real SQL locking and the held native-file
                # sync below. It does not test SQLite power-loss durability.
                store.conn.execute('PRAGMA synchronous=OFF')
                initialize_consumer(store, _pristine_current_schema)
                store.conn.execute('PRAGMA wal_autocheckpoint=0')
                opportunity = choice_evidence.issue(store, 'albums', [{'id': 'album'}], 100,
                    context={}, opportunity_id='held-menu')
                ledger = store.rec_ledger.evidence()
                app = create_app(store, lambda: {}, now_fn=lambda: 101, background_work=False)

                def hold_native_sync(path):
                    if path == ledger.directory and not held.is_set():
                        held.set()
                        assert release.wait(15), 'Controlled native write was not released'
                        # Exercise both resources at the resumed write, not only path existence.
                        assert store.conn.execute('SELECT 1').fetchone()[0] == 1
                    original_sync(path)

                monkeypatch.setattr(evidence_ledger, '_sync_directory', hold_native_sync)

                def publish():
                    observations['published'] = choice_evidence.safe_respond(
                        store, opportunity, 'choice', 101, option_id='album', action_id='one-choice')
                    return observations['published']

                @app.post('/held-response')
                def respond(background: BackgroundTasks):
                    if work_kind == 'background':
                        background.add_task(publish)
                        return {'queued': True}
                    return {'published': publish()}

                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    port = listener.getsockname()[1]
                    server = uvicorn.Server(uvicorn.Config(app, log_level='warning',
                                                          ws='none', loop='asyncio', http='h11'))

                    def send_request():
                        with urlopen(Request(f'http://127.0.0.1:{port}/held-response',
                                             method='POST'), timeout=15) as response:
                            return response.status, json.load(response)

                    def release_after_shutdown():
                        try:
                            deadline = time.monotonic() + 5
                            while not server.should_exit:
                                assert time.monotonic() < deadline, 'Shutdown was not requested'
                                time.sleep(.01)
                            # Verify admission really stops while the existing write drains.
                            deadline = time.monotonic() + 2
                            while True:
                                try:
                                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                                        pass
                                except OSError:
                                    break
                                assert time.monotonic() < deadline, 'Server still admits connections'
                                time.sleep(.01)
                            assert draining.wait(2), 'Server owner did not start draining'
                            # Timed lifetime waits return immediately below, as
                            # though their deadline elapsed with work still live.
                            # The owner must remain until the real write resumes.
                            assert not owner_finished.wait(.01), 'Server owner abandoned a live write'
                            assert ledger.directory.is_dir()
                            assert store.conn.execute('SELECT 1').fetchone()[0] == 1
                            observations['retained_after_shutdown_wait_started'] = True
                        finally:
                            release.set()

                    expected = {'failure': AssertionError, 'interrupt': KeyboardInterrupt}.get(ending)
                    try:
                        with pytest.raises(expected) if expected else nullcontext():
                            with LiveServer(server, sockets=[listener]) as owner:
                                finished_wait, thread_join = owner._finished.wait, owner.thread.join

                                def wait_for_completion(timeout=None):
                                    draining.set()
                                    return finished_wait(0 if timeout is not None else None)

                                def join_server(timeout=None):
                                    draining.set()
                                    return thread_join(0 if timeout is not None else None)

                                monkeypatch.setattr(owner._finished, 'wait', wait_for_completion)
                                monkeypatch.setattr(owner.thread, 'join', join_server)
                                response = clients.submit(send_request)
                                assert held.wait(5), 'Request did not reach the native evidence write'
                                releaser = clients.submit(release_after_shutdown)
                                if expected:
                                    raise expected('Controlled test body termination')
                    finally:
                        owner_finished.set()
                        release.set()
                    releaser.result(timeout=2)
                    assert response.result(timeout=2) == (200,
                        {'queued' if work_kind == 'background' else 'published': True})
                    assert observations['published'] is True
                    assert not owner.thread.is_alive()
                    assert not server.server_state.tasks
                    assert not app.state.ctx.work.active
                    assert not app.state.ctx.work.accepting

                assert ledger.get('response', 'one-choice')['option_id'] == 'album'
                assert not choice_evidence.respond(store, opportunity, 'choice', 102,
                                                   option_id='album', action_id='one-choice')
                assert len(ledger.records('response')) == 1
            finally:
                release.set()
                store.conn.close()
        assert not directory.exists()
    assert 'listening response measurement failed' not in caplog.text
    assert not [record for record in caplog.records if record.levelno >= 40]
    request.node.user_properties.append(('teardown_observations', json.dumps(observations)))


def test_startup_failure_stops_its_server_thread():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def failed_startup(app):
        raise RuntimeError('Controlled startup failure')
        yield

    app = FastAPI(lifespan=failed_startup)
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical'))
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError, match='did not start'):
        LiveServer(server, startup_timeout=1)
    assert server.should_exit
    assert not [thread for thread in set(threading.enumerate()) - before
                if thread.name == 'test-asgi-server']
