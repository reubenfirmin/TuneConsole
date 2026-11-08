"""Real startup admission, rollback, retry and analytics have separate owners."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
from jinja2 import FileSystemBytecodeCache
import pytest

from tests.rebuild_startup_support import (
    admitted_source, assert_original, gate_copy, startup_application,
    startup_destination, startup_development_source,
)
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import build
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.core.migrations import regular, runner
from yt_playlist.rec.choice_evidence import STORAGE_PENDING


@pytest.fixture(scope='module')
def startup_templates(tmp_path_factory):
    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('startup-template-bytecode')))


def test_startup_converter_output_has_current_format_and_real_catalog(startup_destination, tmp_path):
    with closing(sqlite3.connect(tmp_path / 'converter-input.db')) as conn:
        build._configure(conn)
        with conn:
            conn.execute('BEGIN')
            startup_destination.initialize(conn)
        conn.row_factory = sqlite3.Row
        assert build._format(conn) == 107
        assert [row[0] for row in conn.execute('SELECT id FROM accounts ORDER BY id')] == [11, 29]
        assert [row[0] for row in conn.execute('SELECT video_id FROM playlist_items '
            'WHERE playlist_id=41 ORDER BY position')] == ['video-19', 'video-3', 'video-8']
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module', params=[
    ('production64', False), ('production64', True),
    ('development93', False), ('development93', True),
], ids=['production64-success', 'production64-reopening', 'development93-success', 'development93-reopening'])
def startup_input(request, tmp_path_factory):
    name, failure = request.param
    template = request.getfixturevalue('production64_template' if name == 'production64'
                                       else 'startup_development_source')
    root = tmp_path_factory.mktemp('startup-owner')
    with admitted_source((name, template), root) as (store, released):
        if not released:
            store.settings.set_setting('before-layout-rebuild', 'preserved')
        yield SimpleNamespace(store=store, root=root, released=released, failure=failure,
            history=list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))),
            before=sorted(store.conn.iterdump()), inode=store._db_path.stat().st_ino)


def test_startup_admission_preserves_history_and_pending_cleanup(startup_input):
    state, store = startup_input, startup_input.store
    assert store.activity_paused == state.released
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == int(state.released)
    assert store.settings.get_setting(STORAGE_PENDING) == '1'
    assert [playlist.id for playlist in store.playlists.get_playlists()] == [41, 73]
    with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, 64 if state.released else 93)
    assert db_handles.state(store._db_path) is None
    assert not store.conn.in_transaction


def test_completed_startup_input_has_current_format_and_real_split_checkpoint(
        startup_development_source, startup_destination, tmp_path):
    source = tmp_path / 'source93.db'
    with closing(sqlite3.connect(source)) as conn:
        startup_development_source.backup(conn)
        with conn:
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STORAGE_PENDING, '1'))
    output = tmp_path / 'candidate.db'
    report = build.construct(source, output, startup_destination, runtime_only=True)
    assert report['analytics_pending']
    assert report['rows'] == {'completed-runtime-input': 3}
    with closing(sqlite3.connect(output)) as conn:
        conn.row_factory = sqlite3.Row
        assert build._format(conn) == 107
        regular.validate(conn)
        checkpoint = build._checkpoint(conn)
        assert checkpoint['stage'] == 1
        assert checkpoint['stages'] == ['completed-runtime-input', 'completed-analytics-input']
        assert not conn.execute("SELECT 1 FROM settings WHERE key='startup-analytics-finished'").fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def startup_copy(startup_input, startup_destination, startup_templates):
    state = startup_input
    with pytest.MonkeyPatch.context() as patch:
        from yt_playlist.providers import genres
        from yt_playlist.web import app as web_app
        patch.setattr(genres, 'configure', lambda *args, **kwargs: None)
        templates = web_app.Jinja2Templates
        def cached_templates(*args, **kwargs):
            result = templates(*args, **kwargs)
            result.env.bytecode_cache = startup_templates
            return result
        patch.setattr(web_app, 'Jinja2Templates', cached_templates)
        with startup_application(state.store, state.root, patch, startup_destination) as application:
            gate_copy(application, patch, fail=state.failure)
            with TestClient(application.app, base_url='http://127.0.0.1',
                            backend_options={'use_uvloop': True}) as client:
                try:
                    assert application.copying.wait(10), application.ctx.storage_maintenance.error
                    yield SimpleNamespace(**vars(state), application=application, client=client,
                                          maintenance=application.ctx.storage_maintenance)
                finally:
                    application.ctx.storage_maintenance.stop()
                    application.copy_release.set()
                    application.staging_release.set()
                    application.analytics_release.set()


def test_startup_copy_fences_writes_and_serves_the_original(startup_copy):
    state, store = startup_copy, startup_copy.store
    assert store.activity_paused and state.maintenance.active
    assert not state.maintenance.ready.is_set() and not state.application.started
    assert store._evidence_recovery_pending
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert state.client.get('/playlist/41').status_code == 200
    status = state.client.get('/storage-maintenance/status').json()
    assert status['active'] and not status['recording']
    assert status['evidence_maintenance']['pending']
    with pytest.raises(StorageCompacting):
        store.settings.set_setting('during-copy', 'forbidden')
    assert store.settings.get_setting('during-copy') is None


@pytest.fixture(scope='module')
def startup_built(startup_copy):
    state = startup_copy
    state.application.copy_release.set()
    assert state.application.staged.wait(10), state.maintenance.error
    return state


def test_startup_build_keeps_original_fenced_until_installation(startup_built):
    state, store = startup_built, startup_built.store
    assert store.activity_paused and state.maintenance.active
    assert not state.maintenance.ready.is_set() and not state.application.started
    assert not state.application.analytics_entered.is_set()
    assert store._db_path.stat().st_ino == state.inode
    saved = db_handles.state(store._db_path)
    assert saved['phase'] == 'staging' and saved['analytics_pending']
    assert saved['copy']['rows'] == {'completed-runtime-input': 3}


@pytest.fixture(scope='module')
def startup_after_copy(startup_built):
    state = startup_built
    state.application.staging_release.set()
    if state.failure:
        state.maintenance.join(10)
        assert state.maintenance.phase == 'deferred', state.maintenance.error
    else:
        assert state.application.analytics_entered.wait(10), state.maintenance.error
    return state


def test_startup_reopening_failure_restores_exact_source_before_retry(startup_after_copy):
    state, store = startup_after_copy, startup_after_copy.store
    assert not state.application.started and not state.maintenance.ready.is_set()
    if state.failure:
        assert state.application.failures == ['reopening']
        assert 'controlled reopen failure' in state.maintenance.error
        assert store.activity_paused == state.released
        assert store._evidence_recovery_pending
        assert store.settings.get_setting(STORAGE_PENDING) == '1'
        assert not state.application.analytics_entered.is_set()
        assert store._db_path.stat().st_ino == state.inode
        assert sorted(store.conn.iterdump()) == state.before
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == state.history
    else:
        assert not store.activity_paused and not state.maintenance.active
        assert store.settings.get_setting(STORAGE_PENDING) == '0'
    assert not store.conn.in_transaction


def test_startup_cutover_or_rollback_keeps_playlist_browsing(startup_after_copy):
    assert startup_after_copy.client.get('/playlist/41').status_code == 200


@pytest.fixture(scope='module')
def startup_analytics(startup_after_copy):
    state = startup_after_copy
    if state.failure:
        if not state.released:
            state.store.settings.set_setting('after-rejected-copy', 'preserved')
        assert state.client.post('/storage-maintenance/rebuild').status_code == 202
        assert state.application.analytics_entered.wait(10), state.maintenance.error
    return state


def test_startup_analytics_admits_writes_and_keeps_workers_waiting(startup_analytics):
    state, store = startup_analytics, startup_analytics.store
    assert not store.activity_paused and not state.maintenance.active
    assert not state.maintenance.ready.is_set() and not state.application.started
    assert store.settings.get_setting(STORAGE_PENDING) == '0'
    assert store.settings.get_setting('accepted-during-analytics') == 'preserved'
    assert state.client.get('/storage-maintenance/status').json()['recording']


@pytest.fixture(scope='module')
def startup_completed(startup_analytics):
    state = startup_analytics
    state.application.analytics_release.set()
    state.maintenance.join(10)
    assert state.maintenance.phase == 'done', state.maintenance.error
    assert state.application.workers_started.wait(5)
    return state


def test_startup_finishes_analytics_before_workers_and_retains_original_custody(startup_completed):
    state, store = startup_completed, startup_completed.store
    assert state.maintenance.ready.is_set() and not store.activity_paused
    assert not store._evidence_recovery_pending
    assert store.settings.get_setting('startup-analytics-finished') == '1'
    assert store.settings.get_setting('accepted-during-analytics') == 'preserved'
    store.settings.set_setting('after-upgrade', 'accepted')
    assert state.application.started == ['recommendations', 'enrichment']
    assert_original(store, state.history)
    assert store.conn.execute('SELECT max(version) FROM database_rebuild_source_history').fetchone()[0] == (
        64 if state.released else 93)
    if not state.released:
        assert store.settings.get_setting('before-layout-rebuild') == 'preserved'
        if state.failure:
            assert store.settings.get_setting('after-rejected-copy') == 'preserved'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
