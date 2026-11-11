"""Reader lifetime, committed visibility and bounded analytics admission."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from yt_playlist.core import activity, db_reads
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.store import Store
from yt_playlist.repos.base import Repo, snapshot_read


@pytest.fixture(params=['file', 'memory'])
def database(request):
    store = request.getfixturevalue('file_backed_store' if request.param == 'file' else 'store')
    store.settings.set_setting('input', 'old')
    return store


class Analytics(Repo):
    @snapshot_read
    def value(self):
        return self.nested_value()

    @snapshot_read
    def nested_value(self):
        return self.conn.execute("SELECT value FROM settings WHERE key='input'").fetchone()[0]


def test_analytics_does_not_capture_publication_revisions_and_reuses_reader(database, monkeypatch):
    def capture(*args, **kwargs):
        pytest.fail('Read-only analytics copied publication revisions')
    monkeypatch.setattr(database._snapshot_revisions, 'capture', capture)
    original, connections = database._make_reader, []
    def make_reader():
        reader = original()
        connections.append(reader.conn)
        return reader
    monkeypatch.setattr(database, '_make_reader', make_reader)
    assert Analytics(database).value() == 'old'
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


def test_in_memory_private_reader_preserves_published_group_membership(store):
    from tests.group_support import recordings
    from tests.test_group_preparation import seed
    from yt_playlist.rec import group_preparation

    tracks = recordings([('House', 3, .1)])
    seed(store, tracks)
    assert group_preparation.Preparation(store).step(1000)
    expected = store.taste_groups.head(members=True)
    assert {key for group in expected['groups'] if group['leaf']
            for key in group['members']} == {track.key for track in tracks}

    with store.read_view() as reader:
        assert reader.taste_groups.head(members=True) == expected
        assert reader.taste_groups.definitions(expected['generation'], members=True) == expected['groups']


def test_file_reader_pins_committed_state_without_copying_database(file_backed_store, tmp_path, monkeypatch):
    store = file_backed_store
    store.settings.set_setting('input', 'old')
    def backup(*args, **kwargs):
        pytest.fail('File-backed analytics copied the database')
    monkeypatch.setattr(DiagnosticConnection, 'backup', backup)
    before = set(tmp_path.iterdir())
    try:
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            assert reader.conn is not store.conn
            store.settings.set_setting('input', 'new')
            assert reader.settings.get_setting('input') == 'old'
            with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
                reader.conn.execute("INSERT INTO settings VALUES ('forbidden','write')")
        assert store.settings.get_setting('forbidden') is None
        assert Analytics(store).value() == 'new'
        added = set(tmp_path.iterdir()) - before
        assert added <= {tmp_path / ('store.db.' + domain) for domain in (
            'policies', 'groups', 'recommendations', 'evidence', 'served', 'collections',
            'metadata', 'metadata-runtime', 'archives', 'vibe-provenance', 'vibe-models', 'profiles',
            'embeddings', 'legacy-journals', 'policy-history')}
        # Empty owner stores still establish their reader-lifetime lock before
        # pinning SQL. They must not create a database or output snapshot.
        for directory in added:
            assert {path.name for path in directory.iterdir()} == {'.readers.lock'}
    finally:
        store.conn.close()


def test_releasing_reader_allows_wal_checkpoint_to_finish(file_backed_store):
    store = file_backed_store
    store.settings.set_setting('input', 'old')
    try:
        store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            for n in range(5):
                store.settings.set_setting('input', str(n))
            assert reader.settings.get_setting('input') == 'old'
            _, frames, checkpointed = store.conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            assert frames > checkpointed
        _, frames, checkpointed = store.conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        assert frames == checkpointed
    finally:
        store.conn.close()


def test_setup_failure_closes_connection_and_releases_capacity(database, monkeypatch):
    connections = []
    def configure(reader):
        connections.append(reader.conn)
        raise ValueError('reader setup failed')
    database._analytics_readers = threading.BoundedSemaphore(1)
    monkeypatch.setattr(Store, '_configure_connection', configure)
    with pytest.raises(ValueError, match='setup failed'), database.read_view():
        pytest.fail('Reader setup unexpectedly succeeded')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
    assert database._analytics_readers.acquire(blocking=False)
    database._analytics_readers.release()


@pytest.mark.parametrize('failure', [ValueError, activity.WorkClosing])
def test_error_closes_reader_and_releases_capacity(database, failure):
    database._analytics_readers = threading.BoundedSemaphore(1)
    with pytest.raises(failure), database.read_view() as reader:
        raise failure('stop')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        reader.conn.execute('SELECT 1')
    assert database._analytics_readers.acquire(blocking=False)
    database._analytics_readers.release()


def test_reader_limit_waits_without_holding_serving_lock(database):
    database._analytics_readers = threading.BoundedSemaphore(1)
    waiting, entered = threading.Event(), threading.Event()
    def queued():
        waiting.set()
        with database.read_view() as reader:
            entered.set()
            return reader.settings.get_setting('input')
    with ThreadPoolExecutor(2) as pool:
        with database.read_view():
            pending = pool.submit(queued)
            assert waiting.wait(2)
            assert not entered.wait(0.05)
            assert pool.submit(database.settings.get_setting, 'input').result(2) == 'old'
        assert pending.result(2) == 'old'
    assert entered.is_set()


def test_query_timeout_closes_connection_and_releases_capacity(database, monkeypatch):
    clock = [activity.time.monotonic()]
    monkeypatch.setattr(activity.time, 'monotonic', lambda: clock[0])
    database._analytics_readers = threading.BoundedSemaphore(1)
    with pytest.raises(activity.WorkBudgetExceeded), database.read_view() as reader:
        clock[0] += db_reads.READ_SECONDS + 1
        reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
            'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        reader.conn.execute('SELECT 1')
    assert database._analytics_readers.acquire(blocking=False)
    database._analytics_readers.release()


def test_waiting_for_capacity_is_also_bounded(database, monkeypatch):
    monkeypatch.setattr(db_reads, 'READ_SECONDS', 0.05)
    database._analytics_readers = threading.BoundedSemaphore(1)
    assert database._analytics_readers.acquire(blocking=False)
    try:
        with pytest.raises(activity.WorkBudgetExceeded), database.read_view():
            pytest.fail('Reader admitted without capacity')
    finally:
        database._analytics_readers.release()
    monkeypatch.setattr(db_reads, 'READ_SECONDS', 10)
    with database.read_view() as reader:
        assert reader.settings.get_setting('input') == 'old'


def test_worker_snapshot_outlives_request_deadline_but_cancellation_releases_it(database, monkeypatch):
    clock = [activity.time.monotonic()]
    cancelled = [False]
    monkeypatch.setattr(activity.time, 'monotonic', lambda: clock[0])
    database._analytics_readers = threading.BoundedSemaphore(1)
    with (pytest.raises(activity.WorkClosing), activity.cancellable_work(lambda: cancelled[0]),
          database.priority.background(activity.check_stopping)):
        with database.read_view() as reader:
            clock[0] += db_reads.READ_SECONDS + 1
            assert reader.settings.get_setting('input') == 'old'
            cancelled[0] = True
            reader.settings.get_setting('input')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        reader.conn.execute('SELECT 1')
    assert database._analytics_readers.acquire(blocking=False)
    database._analytics_readers.release()


def test_nested_budget_does_not_extend_callers_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, 'monotonic', lambda: clock[0])
    with pytest.raises(activity.WorkBudgetExceeded), activity.work_budget(2):
        with activity.work_budget(10):
            clock[0] += 3
            activity.check_cancellable_work()


def test_read_inside_owned_write_transaction_keeps_uncommitted_visibility(database, monkeypatch):
    def make_reader():
        pytest.fail('Transaction read opened another connection')
    monkeypatch.setattr(database, '_make_reader', make_reader)
    with database._lock:
        database.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='input'")
        try:
            assert Analytics(database).value() == 'uncommitted'
        finally:
            database.conn.rollback()
    assert database.settings.get_setting('input') == 'old'


@pytest.mark.parametrize('operation', ['summary', 'counts', 'playlist'])
def test_slow_analytics_allows_foreground_write(file_backed_store, monkeypatch, operation):
    store = file_backed_store
    store.tracks.upsert_track('v', 'Song', 'Artist', 'Release', 100, album_browse_id='B')
    started, release = threading.Event(), threading.Event()
    original = store._make_reader
    def make_reader():
        reader = original()
        def trace(sql):
            if 'WITH membership AS MATERIALIZED' in sql or 'WITH observations AS' in sql:
                started.set()
                assert release.wait(5), 'Foreground write blocked behind analytics'
        reader.conn.set_trace_callback(trace)
        return reader
    monkeypatch.setattr(store, '_make_reader', make_reader)
    def calculate():
        if operation == 'playlist':
            return store.charts.get_playlist_playback_stats()
        method = (store.collection.album_listening_summary if operation == 'summary'
                  else store.collection.album_listening_counts)
        return method(['B'])
    try:
        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(calculate)
            try:
                assert started.wait(5)
                assert pool.submit(store.settings.set_setting, 'foreground', 'saved').result(2) is None
            finally:
                release.set()
            assert pending.result(5) is not None
        assert store.settings.get_setting('foreground') == 'saved'
    finally:
        store.conn.close()
