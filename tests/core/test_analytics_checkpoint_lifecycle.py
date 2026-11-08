"""Actual checkpoint recovery and durability admission with completed domain inputs."""
from contextlib import closing
import sqlite3
import threading

import pytest

from tests.core.test_db_rebuild_runtime_first import analytics_contract
from tests.genre_catalog_support import bound_catalog
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core import db_handles
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.db_rebuild import build, destination, job, source
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Storage and SQL lifecycle owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('analytics-lifecycle-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        bound_catalog(patch)
        yield


def test_completed_coordination_inputs_keep_real_source_history_and_distinct_analytics_batches(
        production64_template, analytics_contract):
    assert source.admit(production64_template) == 64
    assert production64_template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert production64_template.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8
    assert analytics_contract.runtime_stages == 1
    assert [stage.name for stage in analytics_contract.stages[1:]] == ['served-facts', 'completed-analytics-input']


@pytest.fixture(scope='module', params=['restart', 'batch', 'completion'])
def branch(request, production64_template, analytics_contract, tmp_path_factory):
    path = tmp_path_factory.mktemp('analytics-'+request.param) / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        production64_template.backup(conn)
        conn.execute('PRAGMA journal_mode=WAL')
    state = dict(path=path, store=Store(path), phase=request.param, failures=[], threads=[],
        committing=threading.Event(), release=threading.Event(), written=threading.Event())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(destination, 'for_source', lambda *args, **kwargs: analytics_contract)
        state['patch'] = patch
        try:
            yield state
        finally:
            state['release'].set()
            for thread in state['threads']:
                thread.join(20)
                assert not thread.is_alive()
            if 'maintenance' in state:
                state['maintenance'].stop()
                state['maintenance'].join(15)
            state['store'].conn.close()


@pytest.fixture(scope='module')
def admitted(branch):
    branch['store'].init_schema()
    return branch


def test_released_admission_preserves_installed_history_before_runtime_publication(admitted):
    store = admitted['store']
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64
    assert store.activity_paused
    assert store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8


@pytest.fixture(scope='module')
def runtime(admitted):
    admitted['report'] = job.run(admitted['store'], rebuild=True)
    return admitted


def test_actual_file_cutover_opens_runtime_with_a_committed_analytics_checkpoint(runtime):
    store, report = runtime['store'], runtime['report']
    assert report['phase'] == 'complete' and report['analytics_pending']
    assert not store.activity_paused
    assert store.conn.execute('SELECT document FROM '+build.CHECKPOINT).fetchone()
    assert store.playlists.get_playlist_track_ids(41) == [119, 103, 108]
    store.settings.set_setting('accepted-after-cutover', 'keep me')


@pytest.fixture(scope='module')
def interrupted(runtime):
    state, store = runtime, runtime['store']
    if state['phase'] == 'restart':
        def progress(phase, detail):
            if detail['stage'] == 'served-facts':
                raise InterruptedError('committed analytics checkpoint')

        with pytest.raises(InterruptedError, match='committed analytics checkpoint'):
            job.finish_analytics(store, progress=progress)
        return state

    checkpoint, finish = build._checkpoint, DiagnosticConnection._finish
    connections, held = set(), False

    def marked(conn, value=None):
        if value is not None:
            connections.add(conn)
        return checkpoint(conn, value)

    def delayed(conn, sql, target, *args):
        nonlocal held
        complete = conn in connections and not conn.execute(
            'SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()
        if (not held and conn in connections and sql == 'COMMIT' and conn.in_transaction
                and (state['phase'] == 'batch' or complete)):
            held = True
            state['committing'].set()
            assert state['release'].wait(10)
        return finish(conn, sql, target, *args)

    def complete():
        try:
            state['completion'] = job.finish_analytics(store)
        except BaseException as error:
            state['failures'].append(error)

    def edit():
        try:
            store.settings.set_setting('queued-through-durability', 'accepted')
            state['written'].set()
        except BaseException as error:
            state['failures'].append(error)

    store.conn.execute('PRAGMA busy_timeout=25')
    state['patch'].setattr(build, '_checkpoint', marked)
    state['patch'].setattr(DiagnosticConnection, '_finish', delayed)
    analytics = threading.Thread(target=complete)
    state['threads'].append(analytics)
    analytics.start()
    assert state['committing'].wait(10)
    state['edit'] = edit
    return state


def test_actual_interrupted_checkpoint_or_held_commit_preserves_browsing_and_write_safety(interrupted):
    state, store = interrupted, interrupted['store']
    assert not store.activity_paused
    assert store.playlists.get_playlist_track_ids(41) == [119, 103, 108]
    if state['phase'] == 'restart':
        assert db_handles.state(state['path'])['analytics_pending']
        assert store.settings.get_setting('first-analytics-commit') == 'retained'
        return
    with closing(sqlite3.connect(store._db_path, timeout=0)) as probe:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            probe.execute('BEGIN IMMEDIATE')
    writer = threading.Thread(target=state['edit'])
    state['threads'].append(writer)
    writer.start()
    assert not state['written'].wait(.15)
    assert not state['failures']


@pytest.fixture(scope='module')
def recovered(interrupted):
    state = interrupted
    if state['phase'] == 'restart':
        state['store'].conn.close()
        store = state['store'] = Store(state['path'])
        store.init_schema()
        state['patch'].setattr(destination, 'signature', lambda: 'unrelated-application-edit')
        maintenance = state['maintenance'] = StorageMaintenance(store)
        maintenance.start()
        maintenance.join(15)
        assert maintenance.phase == 'done', maintenance.error
        assert maintenance.ready.is_set()
        assert maintenance.report['id'] == state['report']['id']
        assert not maintenance.report['analytics_pending']
    else:
        state['release'].set()
        for thread in state['threads']:
            thread.join(20)
            assert not thread.is_alive()
        assert not state['failures'] and state['written'].is_set()
        assert not state['completion']['analytics_pending']
    return state


def test_real_recovery_reuses_the_checkpoint_and_preserves_every_accepted_edit(recovered):
    store = recovered['store']
    assert store.settings.get_setting('accepted-after-cutover') == 'keep me'
    assert store.settings.get_setting('first-analytics-commit') == 'retained'
    assert store.settings.get_setting('startup-analytics-finished') == '1'
    assert not db_handles.state(recovered['path'])['analytics_pending']
    assert not store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()
    if recovered['phase'] != 'restart':
        assert store.settings.get_setting('queued-through-durability') == 'accepted'


def test_fresh_reopen_retains_the_completed_runtime_and_queued_durable_write(recovered):
    store = Store(recovered['path'])
    with closing(store.conn):
        store.init_schema()
        assert store.settings.get_setting('accepted-after-cutover') == 'keep me'
        assert store.settings.get_setting('startup-analytics-finished') == '1'
        if recovered['phase'] != 'restart':
            assert store.settings.get_setting('queued-through-durability') == 'accepted'
        assert store.playlists.get_playlist_track_ids(41) == [119, 103, 108]
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
