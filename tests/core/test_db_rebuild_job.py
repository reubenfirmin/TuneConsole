"""Direct destination contracts share the compactor's admission and installer.

This controlled current-layout fixture proves orchestration and rollback. It is
not the production-schema importer or optimized destination acceptance.
"""
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import build, install, job
from yt_playlist.core.migrations import current, registry


class ProcessDied(BaseException):
    pass


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('outcome', ['complete', 'reject', 'resume', 'restart', 'changed'])
def test_direct_destination_uses_verified_snapshot_and_shared_install(installed, outcome, tmp_path, monkeypatch):
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
        assert Path(source.execute('PRAGMA database_list').fetchone()[2]).name == 'verified-original.db'
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
            with pytest.raises(ValueError, match='changed values'):
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
