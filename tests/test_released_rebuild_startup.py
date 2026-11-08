"""Released libraries stay available without running populated migrations."""
from contextlib import closing
from fastapi.testclient import TestClient
import pytest
import sqlite3
import threading
from types import SimpleNamespace

from tests.conftest import FakeClient
from tests.db_rebuild_support import production64
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import without_metadata_providers
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core import playback_feedback
from yt_playlist.core.runtime import Runtime
from yt_playlist.core.setup import write_config
from yt_playlist.core.store import Store
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module', autouse=True)
def released_application_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('released-startup-home')))
        yield


def test_released_source_input_keeps_the_real_release_history_and_playlists(production64_template):
    source = production64_template
    assert source.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64
    assert [row[0] for row in source.execute('SELECT id FROM playlists ORDER BY id')] == [41, 73]
    assert not source.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module', params=[False, True])
def released_application(request, production64_template, startup_destination, tmp_path_factory):
    from fastapi import APIRouter
    from yt_playlist.core.db_rebuild import build, destination
    from yt_playlist.providers import genres
    from yt_playlist.web import app as web_app
    from yt_playlist.web.routes import app_update, audio_preview, home, likes, playlists, sync

    # This owner covers released startup, HTTP availability, the maintenance
    # installer and confirmed-feedback replay. Domain conversion is a completed
    # lower input; the real registered converters have independent owners.
    background = request.param
    path = tmp_path_factory.mktemp('released-startup') / 'production64.db'
    with closing(sqlite3.connect(path)) as conn:
        production64_template.backup(conn)
    before = path.read_bytes()
    copying, release = threading.Event(), threading.Event()
    construct = build.construct
    def hold_copy(*args, **kwargs):
        copying.set()
        assert release.wait(30)
        return construct(*args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        patch.setattr(destination, 'for_source', lambda *args, **kwargs: startup_destination)
        patch.setattr(genres, 'configure', lambda *args, **kwargs: None)
        patch.setattr(web_app, 'build_all', lambda ctx: [module.build(ctx)
            for module in (app_update, audio_preview, home, likes, playlists, sync)])
        patch.setattr(web_app, 'build_bridge_route', lambda ctx: APIRouter())
        patch.setattr(web_app, '_install_idle_shutdown', lambda *args: None)
        patch.setattr(build, 'construct', hold_copy)
        patch.setattr(web_app, '_background_sync_loop', lambda *args, **kwargs: None)
        patch.setattr(web_app, '_background_update_loop', lambda *args, **kwargs: None)
        store = Store(path)
        try:
            store.init_schema()
            remote = FakeClient()
            app = create_app(store, lambda: {11: remote, 29: FakeClient()}, background_work=background)
            ctx = app.state.ctx
            patch.setattr(ctx.radio, 'prepare', lambda: None)
            patch.setattr(ctx.rec_worker, 'start_ticker', lambda: None)
            patch.setattr(ctx.rec_worker, 'trigger_mode_refresh', lambda: None)
            patch.setattr(ctx.enrich_worker, 'start_ticker', lambda: None)
            with TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True}) as client:
                if background:
                    assert copying.wait(20)
                yield SimpleNamespace(store=store, client=client, ctx=ctx, remote=remote,
                    release=release, background=background, path=path, before=before)
        finally:
            release.set()
            store.conn.close()


def test_released_application_admits_readonly_playlists_before_copy(released_application):
    state = released_application
    store = state.store
    assert store.activity_paused
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert store.conn.total_changes == 0
    assert [p.id for p in store.playlists.get_playlists()] == [41, 73]
    with pytest.raises(StorageCompacting):
        store.settings.set_setting('forbidden_write', '1')


@pytest.fixture(scope='module')
def released_feedback(released_application):
    state = released_application
    client, store = state.client, state.store
    assert client.get('/').status_code == 200
    assert client.get('/playlist/41').status_code == 200
    status = client.get('/storage-maintenance/status').json()
    assert status['active'] and not status['recording']
    response = client.post('/track/like', data={'video_id': 'video-3', 'on': '1'})
    assert response.status_code == 200
    assert state.remote.rated == [('video-3', 'LIKE')]
    assert playback_feedback.inbox(store).pending() == 1
    assert playback_feedback.drain(store) == 0
    assert client.post('/sync').status_code == 503
    return state


def test_released_http_and_feedback_keep_history_unchanged_while_copy_is_blocked(released_feedback):
    store = released_feedback.store
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64
    assert store.activity_paused
    assert playback_feedback.inbox(store).pending() == 1


@pytest.fixture(scope='module')
def completed_released_application(released_feedback):
    state = released_feedback
    if state.background:
        state.release.set()
        state.ctx.storage_maintenance.join(30)
        assert state.ctx.storage_maintenance.phase == 'done', state.ctx.storage_maintenance.error
        assert state.ctx.storage_maintenance.ready.is_set()
    return state


def test_released_installer_replays_confirmed_feedback_once(completed_released_application):
    state = completed_released_application
    store = state.store
    if state.background:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107
        assert playback_feedback.inbox(store).pending() == 0
        assert store.conn.execute("SELECT status FROM item_ratings WHERE video_id='video-3'").fetchone()[0] == 'LIKE'
    else:
        assert state.path.read_bytes() == state.before


def test_released_source_serves_playlists_without_advancing_history(completed_released_application):
    state = completed_released_application
    if state.background:
        state.store.settings.set_setting('after-background-upgrade', 'accepted')
        assert state.client.get('/playlist/41').status_code == 200
        assert playback_feedback.drain(state.store) == 0
    else:
        assert state.path.read_bytes() == state.before


def test_config_load_uses_existing_account_ids_without_reconciling(production64, tmp_path, monkeypatch):
    import yt_playlist.core.runtime as runtime
    monkeypatch.setattr(runtime, 'build_client', lambda *args: object())
    config = tmp_path/'config.toml'
    write_config([{'id': 11, 'label': 'Main', 'credential_ref': 'main.json', 'is_master': True},
                  {'id': 29, 'label': 'Other account', 'credential_ref': 'other.json'}], config)
    store = Store(production64)
    try:
        store.init_schema()
        rt = Runtime(store, config, tmp_path)
        rt.load()
        assert rt.configured and set(rt.clients()) == {11, 29}
        assert store.conn.total_changes == 0
    finally:
        store.conn.close()


def test_failed_released_upgrade_can_be_retried_through_maintenance_route(production64,monkeypatch):
    from yt_playlist.core.db_rebuild import build
    construct = build.construct
    attempts = []
    def first_attempt_fails(*args,**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError('controlled candidate rejection')
        return construct(*args,**kwargs)
    monkeypatch.setattr(build,'construct',first_attempt_fails)
    store = Store(production64)
    try:
        store.init_schema()
        app = create_app(store,lambda: {11:FakeClient(),29:FakeClient()},background_work=False)
        maintenance = app.state.ctx.storage_maintenance
        with TestClient(app,base_url='http://127.0.0.1') as client:
            maintenance.start()
            maintenance.join(30)
            assert maintenance.phase == 'deferred'
            assert client.get('/storage-maintenance/status').json()['warning']
            assert client.get('/playlist/41').status_code == 200
            assert client.post('/storage-maintenance/rebuild').status_code == 202
            maintenance.join(30)
            assert maintenance.phase == 'done',maintenance.error
            assert len(attempts) == 2 and not store.activity_paused
    finally:
        store.conn.close()
