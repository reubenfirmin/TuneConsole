"""Every maintenance publication checkpoints outside the serving lock after atomic commit."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from yt_playlist.core import db_checkpoint, db_snapshot
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.store import Store


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'publication.db')
    store.init_schema()
    store.conn.execute('PRAGMA wal_autocheckpoint=17')
    yield store
    store.conn.close()


@pytest.mark.parametrize('failure', [None, 'rollback', 'cancel', 'stale'])
def test_checkpoint_observes_finished_transaction_without_holding_serving_lock(database, monkeypatch, failure):
    store = database
    store.settings.set_setting('model', 'old')
    synchronous = store.conn.execute('PRAGMA synchronous').fetchone()[0]
    observed = []

    def read_after_publication():
        if not store._lock.acquire(False):
            return 'serving lock held'
        try:
            return store.settings.get_setting('model')
        finally:
            store._lock.release()

    def checkpoint(path):
        assert path == store._db_path
        assert not store.conn.in_transaction
        assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17
        assert store.conn.execute('PRAGMA synchronous').fetchone()[0] == synchronous
        with ThreadPoolExecutor(1) as pool:
            observed.append(pool.submit(read_after_publication).result(2))

    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', checkpoint)
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('model')
        if failure == 'stale':
            store.settings.set_setting('model', 'latest')

        def publish():
            assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 0
            assert store.conn.execute('PRAGMA synchronous').fetchone()[0] == synchronous
            store.settings.publish_values({'model': 'new'})
            if failure == 'rollback':
                raise ValueError('publication failed')
            if failure == 'cancel':
                raise WorkClosing('cancelled')

        if failure in ('rollback', 'cancel'):
            with pytest.raises(WorkClosing if failure == 'cancel' else ValueError):
                snapshot.publish(publish)
        else:
            assert snapshot.publish(publish) == (failure is None)
    assert observed == ([] if failure else ['new'])
    assert store.settings.get_setting('model') == ('latest' if failure == 'stale' else 'old' if failure else 'new')
    assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17


def test_checkpoint_error_does_not_report_a_committed_model_as_failed(database, monkeypatch):
    store = database
    with store.read_snapshot() as snapshot:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('checkpoint unavailable')

        monkeypatch.setattr(db_checkpoint.sqlite3, 'connect', unavailable)
        assert snapshot.publish(lambda: store.settings.publish_values({'model': 'new'}))
    assert store.settings.get_setting('model') == 'new'
    assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17


def test_explicit_inline_publication_keeps_existing_checkpoint_policy(database, monkeypatch):
    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', lambda path: pytest.fail('unexpected checkpoint'))
    with database.read_snapshot() as snapshot:
        assert snapshot.publish(lambda: database.settings.publish_values({'model': 'new'}),
                                defer_checkpoint=False)
    assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17


@pytest.mark.parametrize('projection', ['trends', 'bundles'])
def test_projection_outputs_and_settings_are_committed_before_checkpoint(database, monkeypatch, projection):
    surface = 'trend_rollups' if projection == 'trends' else 'mode_bundles'
    observed = []

    def checkpoint(path):
        assert path == database._db_path
        assert not database.conn.in_transaction
        # A foreground reader sees matching output and metadata before checkpoint work.
        def read():
            if not database._lock.acquire(False):
                return 'serving lock held'
            try:
                with database.read_snapshot() as snapshot:
                    return (snapshot.store.rec_proposals.get_proposals(surface),
                            snapshot.store.settings.get_setting('projection-version'))
            finally:
                database._lock.release()
        with ThreadPoolExecutor(1) as pool:
            observed.append(pool.submit(read).result(2))

    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', checkpoint)
    with database.read_snapshot() as snapshot:
        def publish():
            assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 0
            if projection == 'trends':
                database.trends.publish_rollup(
                    [('track', 'song', 0, 0., 'play_event')], '{"generation":2}', 2., None)
            else:
                database.rec_ledger.publish_prepared_policies([('projection-policy', '{}', 2.)])
                database.rec_proposals.publish_prepared_bundles(
                    '{"generation":2}', '[]', 2.)
            database.settings.publish_values({'projection-version': '2'})
        assert snapshot.publish(publish)
    assert observed == [({'generation': 2}, '2')]
    assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17


def test_unchanged_publication_does_not_checkpoint(database, monkeypatch):
    database.settings.set_setting('model', 'same')
    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', lambda path: pytest.fail('unexpected checkpoint'))
    with database.read_snapshot() as snapshot:
        assert snapshot.publish(lambda: database.settings.publish_values({'model': 'same'}))
    assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17


def test_explicitly_disabled_checkpoint_policy_is_preserved(database, monkeypatch):
    database.conn.execute('PRAGMA wal_autocheckpoint=0')
    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', lambda path: pytest.fail('unexpected checkpoint'))
    with database.read_snapshot() as snapshot:
        assert snapshot.publish(lambda: database.settings.publish_values({'model': 'new'}))
    assert database.settings.get_setting('model') == 'new'
    assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 0


def test_in_memory_publication_does_not_attempt_a_file_checkpoint(monkeypatch):
    store = Store(':memory:')
    store.init_schema()
    try:
        policy = store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0]
        monkeypatch.setattr(db_snapshot, 'checkpoint_wal', lambda path: pytest.fail('unexpected checkpoint'))
        with store.read_snapshot() as snapshot:
            assert snapshot.publish(lambda: store.settings.publish_values({'model': 'new'}))
        assert store.settings.get_setting('model') == 'new'
        assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == policy
    finally:
        store.conn.close()


def test_cancellation_during_policy_cleanup_does_not_discredit_commit(database, monkeypatch):
    original = type(database.conn).execute
    interrupted = False

    def execute(conn, sql, *args):
        nonlocal interrupted
        if conn is database.conn and sql == 'PRAGMA wal_autocheckpoint=17' and not interrupted:
            interrupted = True
            error = sqlite3.OperationalError('interrupted')
            error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise error
        return original(conn, sql, *args)

    monkeypatch.setattr(type(database.conn), 'execute', execute)
    with database.read_snapshot() as snapshot:
        assert snapshot.publish(lambda: database.settings.publish_values({'model': 'new'}))
    assert interrupted
    assert database.settings.get_setting('model') == 'new'
    assert database.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17
