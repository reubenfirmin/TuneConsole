"""Uninstalled analytics must not strand a failed runtime installation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import destination, job, source
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store
from yt_playlist.rec.choice_evidence import STORAGE_PENDING


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These recovery owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('failed-runtime-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module', params=['production64', 'development93'])
def installed_input(request):
    if request.param == 'production64':
        template = request.getfixturevalue('production64_template')
        version = 64
    else:
        template, _seconds = request.getfixturevalue('development93_template')
        version = 93
    return template, version


def test_retry_source_is_the_populated_read_only_installed_format(installed_input):
    template, version = installed_input
    assert source.admit(template) == version
    assert template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert template.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8


@pytest.fixture(scope='module', params=['staging', 'reopening'])
def retry_branch(installed_input, startup_destination, request, tmp_path_factory):
    template, version = installed_input
    path = tmp_path_factory.mktemp(f'retry-{version}-{request.param}') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        template.backup(conn)
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STORAGE_PENDING, '1'))
    store = Store(path)
    with pytest.MonkeyPatch.context() as patch:
        # Catalog conversion is below this recovery boundary. Keep the genuine
        # construct/checkpoint, closed-file installer, rollback and maintenance.
        patch.setattr(destination, 'for_source', lambda path, **kwargs: startup_destination)
        branch = dict(path=path, store=store, version=version, failure=request.param,
                      history=tuple(map(tuple, template.execute('SELECT * FROM schema_migrations ORDER BY version'))))
        try:
            yield branch
        finally:
            branch['store'].conn.close()


@pytest.fixture(scope='module')
def admitted_branch(retry_branch):
    retry_branch['store'].init_schema(defer_metadata=True)
    return retry_branch


def test_failed_retry_branch_is_admitted_without_changing_its_historical_ledger(admitted_branch):
    retry_branch = admitted_branch
    store = retry_branch['store']
    assert tuple(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == retry_branch['history']
    assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


@pytest.fixture(scope='module')
def failed_install(admitted_branch):
    branch = admitted_branch

    def interrupt(phase, detail):
        if phase == branch['failure']:
            raise InterruptedError('runtime was never admitted')

    with pytest.raises(InterruptedError, match='runtime was never admitted'):
        job.run(branch['store'], rebuild=True, progress=interrupt)
    branch['failed'] = db_handles.state(branch['path'])
    return branch


def test_real_staging_or_reopening_failure_keeps_the_original_uninstalled(failed_install):
    state, store = failed_install['failed'], failed_install['store']
    assert state['phase'] == 'failed' and state['analytics_pending']
    assert not Path(state['original']).exists()
    assert tuple(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == failed_install['history']
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == failed_install['version']
    assert store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8


@pytest.fixture(scope='module')
def background_retry(failed_install):
    store = failed_install['store']
    installed, release = threading.Event(), threading.Event()

    def runtime_resumed():
        if not installed.is_set():
            installed.set()
            assert release.wait(10)

    maintenance = StorageMaintenance(store, on_resume=runtime_resumed)
    try:
        maintenance.start()
        assert installed.wait(10), maintenance.error
        failed_install.update(maintenance=maintenance, release=release, retry=maintenance.report)
        yield failed_install
    finally:
        release.set()
        maintenance.stop()
        maintenance.join(10)


def test_real_background_retry_installs_runtime_before_finishing_analytics(background_retry):
    branch = background_retry
    state = db_handles.state(branch['path'])
    assert branch['retry']['id'] != branch['failed']['id']
    assert state['phase'] == 'complete' and state['analytics_pending']
    assert Path(state['original']).is_file()
    assert not branch['store'].activity_paused
    assert not branch['maintenance'].ready.is_set()
    with closing(sqlite3.connect(Path(state['original']).as_uri() + '?mode=ro', uri=True)) as original:
        assert tuple(original.execute('SELECT * FROM schema_migrations ORDER BY version')) == branch['history']


@pytest.fixture(scope='module')
def completed_retry(background_retry):
    branch = background_retry
    branch['release'].set()
    branch['maintenance'].join(10)
    assert branch['maintenance'].phase == 'done', branch['maintenance'].error
    return branch


def test_real_analytics_completion_releases_workers_after_retry(completed_retry):
    branch = completed_retry
    assert branch['maintenance'].ready.is_set()
    assert not db_handles.state(branch['path'])['analytics_pending']
    assert branch['store'].settings.get_setting('startup-analytics-finished') == '1'


def test_retried_runtime_accepts_and_retains_a_real_write_after_fresh_reopen(completed_retry):
    background_retry = completed_retry
    branch = background_retry
    branch['store'].settings.set_setting('accepted-after-staging-retry', 'keep')
    branch['store'].conn.close()
    branch['store'] = Store(branch['path'])
    branch['store'].init_schema()
    assert branch['store'].settings.get_setting('accepted-after-staging-retry') == 'keep'
    assert not db_handles.state(branch['path'])['analytics_pending']
    assert not branch['store'].conn.execute('PRAGMA foreign_key_check').fetchall()
