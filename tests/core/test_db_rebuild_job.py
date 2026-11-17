"""Direct destination contracts share the compactor's admission and installer.

This controlled current-layout fixture proves orchestration and rollback. It is
not the production-schema importer or optimized destination acceptance.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import db_handles, group_admission
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.db_rebuild import build, install, job
from yt_playlist.core.migrations import runner
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageCompacting, StorageMaintenance


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These storage jobs do not start the web application's periodic workers."""
    yield


@pytest.fixture(scope='module')
def installed_sql():
    """Completed empty SQL below the controlled rebuild destination and source."""
    with pytest.MonkeyPatch.context() as inputs, closing(sqlite3.connect(':memory:')) as schema:
        bound_catalog(inputs)
        install_numbered_input(schema, 107)
        schema.execute('PRAGMA query_only=ON')
        yield schema


@pytest.fixture(scope='module')
def destination_sql(installed_sql):
    # The builder owns an open transaction on a worker thread. Supply immutable
    # SQL statements instead of backing up into that active connection.
    statements = tuple(installed_sql.iterdump())
    assert statements[0] == 'BEGIN TRANSACTION;' and statements[-1] == 'COMMIT;'
    return statements[1:-1]


@pytest.fixture
def installed(request, tmp_path, installed_sql, monkeypatch):
    # The explicit startup cases below still construct and admit their Stores.
    # These jobs own actual read-only construction, cutover and reopening, not
    # initial admission or optimistic recommendation snapshot construction.
    assert request.param == 107
    bound_catalog(monkeypatch)
    store = Store(tmp_path / 'installed.db')
    try:
        installed_sql.backup(store.conn)
        store.conn.execute('PRAGMA synchronous=OFF')
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        store._bind_repositories()
        history = list(store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))
        yield store, request.param
        assert list(store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')) == history
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, request.param)
    finally:
        store.conn.close()


class ProcessDied(BaseException):
    pass


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_install_reopens_original_after_close_callback_failure(installed, destination_sql):
    store, _ = installed
    store.settings.set_setting('accepted-before-close', 'preserved')
    original = store._db_path.stat().st_ino
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]

    def initialize(target):
        for sql in destination_sql:
            target.execute(sql)
        target.execute('DELETE FROM schema_migrations')
        target.executemany('INSERT INTO schema_migrations VALUES(?,?,?)', history)

    def copy(source, target, cursor, limit):
        target.execute('INSERT INTO settings VALUES(?,?)', ('accepted-before-close', 'preserved'))
        return build.Batch(rows=1, done=True)

    def fail_close():
        raise RuntimeError('controlled reader retirement failure')

    previous = store.conn
    previous._before_close_callbacks.append(fail_close)
    destination = build.Destination('close-failure-current-v1', initialize,
                                    (build.Stage('settings', copy),), lambda *_: None)
    with pytest.raises(RuntimeError, match='controlled reader retirement failure'):
        job.run(store, destination=destination)
    assert previous._handle_closed
    assert store.conn is not previous and not store.conn._handle_closed
    assert store._db_path.stat().st_ino == original
    assert db_handles.state(store._db_path)['phase'] == 'failed'
    assert not store.activity_paused
    assert store.settings.get_setting('accepted-before-close') == 'preserved'
    store.settings.set_setting('accepted-after-close-failure', 'preserved')
    assert store.settings.get_setting('accepted-after-close-failure') == 'preserved'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('outcome', ['complete', 'reject', 'resume', 'restart', 'changed'])
def test_direct_destination_uses_readonly_original_and_shared_install(
        installed, destination_sql, outcome, tmp_path, monkeypatch):
    store, _ = installed
    store.settings.set_setting('personal', 'Mixed case')
    original = store._db_path.stat().st_ino
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    observations = []
    copied = []
    scratch = None
    if outcome == 'resume':
        scratch = tmp_path/'scratch'
        scratch.mkdir()
        # Exercise recorded cross-filesystem custody regardless of local mounts.
        monkeypatch.setattr(job, '_same_filesystem', lambda *_: False)

    def initialize(target):
        for sql in destination_sql:
            target.execute(sql)
        target.execute('DELETE FROM schema_migrations')
        target.executemany('INSERT INTO schema_migrations VALUES(?,?,?)', history)

    def copy(source, target, cursor, limit):
        assert Path(source.execute('PRAGMA database_list').fetchone()[2]) == store._db_path
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            source.execute("UPDATE settings SET value='changed source' WHERE key='personal'")
        value = source.execute("SELECT value FROM settings WHERE key='personal'").fetchone()[0]
        target.execute('INSERT INTO settings VALUES(?,?)', ('personal', value.upper()))
        copied.append(value)
        return build.Batch(rows=1, done=True)

    def verify(source, target):
        assert target.execute("SELECT value FROM settings WHERE key='personal'").fetchone() == ('MIXED CASE',)
        if outcome == 'reject':
            raise ValueError('Controlled domain preservation failure')

    def progress(phase, detail):
        state = db_handles.state(store._db_path)
        assert state['operation'] == 'rebuild'
        assert state['destination_version'] == 'controlled-current-v1'
        if phase in ('copy', 'verify'):
            assert store.activity_paused
            assert store.settings.get_setting('personal') == 'Mixed case'
        observations.append(phase)
        if (outcome in ('resume', 'restart', 'changed') and detail.get('stage') == 'personal'
                and not state.get('resumed')):
            if outcome == 'restart':
                raise ProcessDied()
            raise InterruptedError('Controlled interruption after committed batch')

    destination = build.Destination('controlled-current-v1', initialize, (build.Stage('personal', copy),), verify)
    if outcome in ('resume', 'restart', 'changed'):
        with pytest.raises(ProcessDied if outcome == 'restart' else InterruptedError):
            job.run(store, destination=destination, scratch=scratch, progress=progress)
        if outcome == 'restart':
            assert store.activity_paused
            store.conn.close()
            install.recover(store._db_path)
            install.reopen(store)
            store.activity_paused = False
        attempt = db_handles.state(store._db_path)['id']
        assert copied == ['Mixed case']
        assert not store.activity_paused
        if outcome == 'changed':
            store.settings.set_setting('personal', 'Newer accepted value')
            with pytest.raises(ValueError, match='source changed'):
                job.run(store, destination=destination, resume=True, progress=progress)
            assert store._db_path.stat().st_ino == original
            assert store.settings.get_setting('personal') == 'Newer accepted value'
        else:
            report = job.run(store, destination=destination, resume=True, progress=progress)
            assert report['id'] == attempt and report['phase'] == 'complete'
            assert report['resumed'] and copied == ['Mixed case']
            assert store.settings.get_setting('personal') == 'MIXED CASE'
            if scratch is not None:
                assert Path(report['work']).parent == scratch
                assert not report['capacity']['same_filesystem']
    elif outcome == 'reject':
        with pytest.raises(ValueError, match='preservation failure'):
            job.run(store, destination=destination, progress=progress)
        assert store._db_path.stat().st_ino == original
        assert store.settings.get_setting('personal') == 'Mixed case'
        assert 'closing' not in observations
    else:
        report = job.run(store, destination=destination, progress=progress)
        assert report['phase'] == 'complete'
        assert report['copy']['destination'] == destination.version
        assert store._db_path.stat().st_ino != original
        assert store.settings.get_setting('personal') == 'MIXED CASE'
        assert 'original_moved' in observations
    assert not store.activity_paused
    store.settings.set_setting('after rebuild', 'accepted')
    assert store.settings.get_setting('after rebuild') == 'accepted'


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('interrupt', [False, True])
def test_background_destination_keeps_serving_and_can_resume_committed_copy(installed, destination_sql, interrupt):
    store, _ = installed
    store.settings.set_setting('personal', 'Mixed case')
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    held, release = threading.Event(), threading.Event()
    copied, resumed_values = [], []
    maintenance = StorageMaintenance(store, on_resume=lambda: resumed_values.append(
        store.settings.get_setting('personal')))

    def initialize(target):
        for sql in destination_sql:
            target.execute(sql)
        target.execute('DELETE FROM schema_migrations')
        target.executemany('INSERT INTO schema_migrations VALUES(?,?,?)', history)

    def copy(source, target, cursor, limit):
        held.set()
        assert release.wait(30)
        value = source.execute("SELECT value FROM settings WHERE key='personal'").fetchone()[0]
        target.execute('INSERT INTO settings VALUES(?,?)', ('personal', value.upper()))
        copied.append(value)
        return build.Batch(rows=1, done=True)

    def verify(source, target):
        assert target.execute("SELECT value FROM settings WHERE key='personal'").fetchone() == ('MIXED CASE',)

    destination = build.Destination('controlled-background-v1', initialize, (build.Stage('personal', copy),), verify)
    maintenance.start_rebuild(destination)
    try:
        assert held.wait(20)
        assert not maintenance.ready.is_set()
        status = maintenance.status()
        assert status['active'] and not status['recording']
        assert status['message'].startswith('Updating library storage.')
        assert status['capturing_feedback']
        assert store.settings.get_setting('personal') == 'Mixed case'
        with pytest.raises(WorkBudgetExceeded, match='using publication'):
            with group_admission.admission(store, blocking=False):
                pytest.fail('Evidence publication entered during database replacement')
        with pytest.raises(StorageCompacting):
            maintenance.start_compaction()
        if interrupt:
            maintenance.stop()
    finally:
        release.set()
        maintenance.join(30)
    if interrupt:
        assert maintenance.phase == 'deferred'
        assert not store.activity_paused
        assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 0
        assert store.settings.get_setting('personal') == 'Mixed case'
        attempt = db_handles.state(store._db_path)['id']
        maintenance.start_rebuild(destination, resume=True)
        maintenance.join(30)
        assert maintenance.report['id'] == attempt
        assert maintenance.report['resumed']
        assert resumed_values == ['Mixed case', 'MIXED CASE']
    else:
        assert resumed_values == ['MIXED CASE']
    assert maintenance.phase == 'done', maintenance.error
    assert maintenance.ready.is_set()
    assert not store.activity_paused and maintenance.status()['recording']
    assert copied == ['Mixed case']
    assert store.settings.get_setting('personal') == 'MIXED CASE'
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
    store.settings.set_setting('after rebuild', 'accepted')
