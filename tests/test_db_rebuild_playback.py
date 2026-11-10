"""Queued playlist playback stays fenced through replacement and rollback."""
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode
from tests.publication_fixture_support import installed as playback_library
from tests.test_deferred_playback_feedback import _pristine_current_schema, file_backed_store, library
from yt_playlist.core import db_handles
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.fixture(autouse=True)
def playback_routes(monkeypatch, home_page_bytecode):
    from yt_playlist.providers import genres
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, playlists

    # Keep actual playback/WebSocket transport, playlist rendering and storage
    # maintenance. Other feature routes and metadata providers are lower inputs.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
                        for module in (app_update, audio_preview, playlists)])
    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


@pytest.mark.parametrize('playback_library', [93], indirect=True)
@pytest.mark.parametrize('failure', [None, 'original_moved', 'reopening', 'repositories'])
def test_queued_play_survives_install_or_rollback(playback_library, playback_routes, monkeypatch, failure):
    # Historical admission is below this cutover workflow. Actual close/reopen,
    # handle/thread fences, copy/replacement and rollback remain the subject.
    store, version = playback_library
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    playlist = store.playlists.upsert_playlist(account, 'RAIN', 'Rainy evening', 2, '', 1)
    tracks = [store.tracks.upsert_track('rain-'+str(i), title, 'Artist', None, 180)
              for i, title in enumerate(('After the rain', 'Evening lights'))]
    store.playlists.set_playlist_tracks(playlist, tracks)
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    inode = store._db_path.stat().st_ino
    app = create_app(store, lambda: {account: FakeClient()}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    entered, release, reading = threading.Event(), threading.Event(), threading.Event()
    original_progress = maintenance._progress
    get_playlists = store.playlists.get_playlists
    bind = store._bind_repositories
    bind_attempts = 0

    def progress(phase, detail=None):
        original_progress(phase, detail)
        if phase == 'original_moved':
            assert not store._db_path.exists() and store.conn._handle_closed
            entered.set()
            assert release.wait(15), 'cutover was not released'
        if phase == failure:
            raise RuntimeError('controlled '+failure+' failure')

    def playlist_read(*args, **kwargs):
        if db_handles.state(store._db_path)['phase'] == 'original_moved':
            available = store._lock.acquire(blocking=False)
            if available:
                store._lock.release()
            assert not available, 'Play must wait for the installation owner'
            reading.set()
        return get_playlists(*args, **kwargs)

    def bind_repositories():
        nonlocal bind_attempts
        bind_attempts += 1
        if failure == 'repositories' and bind_attempts == 1:
            raise RuntimeError('controlled repository reopen failure')
        bind()

    monkeypatch.setattr(maintenance, '_progress', progress)
    monkeypatch.setattr(store.playlists, 'get_playlists', playlist_read)
    monkeypatch.setattr(store, '_bind_repositories', bind_repositories)
    url = 'https://music.youtube.com/playlist?list=RAIN'
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            with client.websocket_connect('/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as extension:
                assert client.post('/storage-maintenance/compact').status_code == 202
                assert entered.wait(15), 'copy did not reach closed-handle replacement'
                with ThreadPoolExecutor(max_workers=1) as worker:
                    pending = worker.submit(client.post, '/play', json={'url': url})
                    try:
                        assert reading.wait(5), 'Play did not reach its serving-lock read'
                        assert not pending.done()
                        assert client.get('/storage-maintenance/status').json()['active']
                    finally:
                        release.set()
                    assert pending.result(timeout=10).json() == {'ok': True}
                frame = extension.receive_json()
                while frame.get('ping'):
                    frame = extension.receive_json()
                assert frame == {'type': 'navigate', 'url': url}
                # A barrier frame establishes that exactly one navigate preceded it.
                assert app.state.ctx.bridge.send_control({'type': 'rebuild-test-barrier'})
                frame = extension.receive_json()
                while frame.get('ping'):
                    frame = extension.receive_json()
                assert frame == {'type': 'rebuild-test-barrier'}
                maintenance.join(15)
                assert maintenance.phase == ('done' if failure is None else 'deferred')
                assert not store.activity_paused
                assert (store._db_path.stat().st_ino != inode) == (failure is None)
                assert [tuple(row) for row in store.conn.execute(
                    'SELECT * FROM schema_migrations ORDER BY version')] == history
                assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
                assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
                store.settings.set_setting('after rebuild', 'accepted')
                assert store.settings.get_setting('after rebuild') == 'accepted'
                assert client.get('/playlist/'+str(playlist)).status_code == 200
    finally:
        release.set()
        maintenance.join(15)


def test_connection_setup_survives_maintenance_starting_before_pairing_is_recorded(library, monkeypatch):
    from yt_playlist.web.routes import bridge as routes

    store, main, other, _, _ = library
    app = create_app(store, lambda: {main: FakeClient(), other: FakeClient()}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    recording, allow_record, recorded = threading.Event(), threading.Event(), threading.Event()
    copying, allow_copy = threading.Event(), threading.Event()
    record_connection = routes._record_connection
    original_progress = maintenance._progress

    def record(ctx):
        recording.set()
        assert allow_record.wait(15)
        record_connection(ctx)
        recorded.set()

    def progress(phase, detail=None):
        original_progress(phase, detail)
        if phase == 'copy':
            copying.set()
            assert allow_copy.wait(15)

    monkeypatch.setattr(routes, '_record_connection', record)
    monkeypatch.setattr(maintenance, '_progress', progress)
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            with client.websocket_connect('/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as extension:
                assert recording.wait(5)
                assert client.post('/storage-maintenance/compact').status_code == 202
                assert copying.wait(15) and store.activity_paused
                allow_record.set()
                assert recorded.wait(5), 'connection setup failed under maintenance admission'
                assert app.state.ctx.bridge.connected
                url = 'https://music.youtube.com/playlist?list=LM'
                assert client.post('/play', json={'url': url}).json() == {'ok': True}
                frame = extension.receive_json()
                while frame.get('ping'):
                    frame = extension.receive_json()
                assert frame == {'type': 'navigate', 'url': url}
                assert store.settings.get_setting('bridge_paired') is None
                allow_copy.set()
                maintenance.join(15)
                assert maintenance.phase == 'done'
    finally:
        allow_record.set()
        allow_copy.set()
        maintenance.join(15)
