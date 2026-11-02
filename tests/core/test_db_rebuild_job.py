"""Direct destination contracts share the compactor's admission and installer.

This controlled current-layout fixture proves orchestration and rollback. It is
not the production-schema importer or optimized destination acceptance.
"""
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_db_rebuild_destination import development93, production64
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import build, install, job
from yt_playlist.core.migrations import current, registry
from yt_playlist.core.storage_maintenance import StorageCompacting, StorageMaintenance


class ProcessDied(BaseException):
    pass


@pytest.mark.parametrize('blocker', ['transaction', 'maintenance', 'evidence', 'compaction'])
def test_pending_evidence_retry_does_not_bypass_live_write_owners(development93, blocker):
    from yt_playlist.core.store import Store

    store = Store(development93)
    try:
        store.init_schema(defer_metadata=True)
        store._evidence_recovery_pending = True
        if blocker == 'transaction':
            store.conn.execute('BEGIN IMMEDIATE')
        elif blocker == 'maintenance':
            store.activity_paused = True
        elif blocker == 'evidence':
            store._evidence_maintenance = 1
        with pytest.raises(RuntimeError, match='idle write admission'):
            job.run(store, rebuild=blocker != 'compaction')
        assert store.conn.in_transaction == (blocker == 'transaction')
        assert db_handles.state(development93) is None
        assert store._evidence_recovery_pending
    finally:
        store.conn.rollback()
        store.conn.close()


@pytest.mark.parametrize(('source_fixture', 'changed'), [
    ('production64', False), ('development93', False), ('development93', True)])
def test_startup_reuses_unchanged_source_and_restarts_after_accepted_edits(request,source_fixture,changed,monkeypatch):
    from yt_playlist.core.store import Store
    path = request.getfixturevalue(source_fixture)
    store = Store(path)
    store.init_schema()
    first = StorageMaintenance(store)
    progress = first._progress
    def interrupted(phase,detail):
        progress(phase,detail)
        if detail.get('stage') == 'copy:playlist_items':
            raise InterruptedError('controlled background checkpoint')
    monkeypatch.setattr(first,'_progress',interrupted)
    try:
        first.start()
        first.join(30)
        assert first.phase == 'deferred',first.error
        state = db_handles.state(path)
        assert state['phase'] == 'failed' and state['failed_phase'] == 'building'
        if changed:
            store.settings.set_setting('accepted-between-attempts', 'preserved')
        second = StorageMaintenance(store)
        second.start()
        second.join(30)
        assert second.phase == 'done',second.error
        if changed:
            assert second.report['id'] != state['id']
            assert store.settings.get_setting('accepted-between-attempts') == 'preserved'
        else:
            assert second.report['id'] == state['id'] and second.report['resumed']
        assert second.ready.is_set() and not store.activity_paused
        assert store.conn.execute('SELECT max(version) FROM database_rebuild_source_history').fetchone()[0] == (
            64 if source_fixture == 'production64' else 93)
        store.settings.set_setting('after-automatic-resume','accepted')
    finally:
        first.stop()
        first.join(30)
        if 'second' in locals():
            second.stop()
            second.join(30)
        store.conn.close()


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('outcome', ['complete', 'reject', 'resume', 'restart', 'changed'])
def test_direct_destination_uses_readonly_original_and_shared_install(installed, outcome, tmp_path, monkeypatch):
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
        current.bootstrap(target, registry.MIGRATIONS, 1000)
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
def test_background_destination_keeps_serving_and_can_resume_committed_copy(installed, interrupt):
    store, _ = installed
    store.settings.set_setting('personal', 'Mixed case')
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    held, release = threading.Event(), threading.Event()
    copied, resumed_values = [], []
    maintenance = StorageMaintenance(store, on_resume=lambda: resumed_values.append(
        store.settings.get_setting('personal')))

    def initialize(target):
        current.bootstrap(target, registry.MIGRATIONS, 1000)
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
