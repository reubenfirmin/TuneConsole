"""Real background entry points leave serving reads available under WAL contention."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
import sqlite3
import threading
import time

import pytest

from yt_playlist.core.activity import cancellable_work, check_cancellable_work, WorkClosing
from yt_playlist.core import provider_frontiers
from yt_playlist.core.store import Store
from yt_playlist.rec import group_roots


ORIGINAL = {'deezer': {'fingerprint': 'original', 'status': 'available'}}
CHANGED = {'deezer': {'fingerprint': 'changed', 'status': 'available'}}
OPERATIONS = ['provider-unchanged', 'provider-changed', 'recommendation-empty',
              'recommendation-write', 'snapshot']


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'background.db')
    store.init_schema()
    store.enrichment_state.reconcile_provider_configuration(ORIGINAL)
    yield store
    store.conn.close()


def operation(store, kind, stack):
    if kind.startswith('provider'):
        configuration = ORIGINAL if kind.endswith('unchanged') else CHANGED
        return lambda: store.enrichment_state.reconcile_provider_configuration(configuration)
    if kind == 'snapshot':
        snapshot = stack.enter_context(store.read_snapshot())
        snapshot.store.settings.get_setting('input')
        return lambda: snapshot.publish(lambda: store.settings.publish_values({'result': 'published'}))
    def write():
        with group_roots.writer(store) as conn:
            if kind == 'recommendation-write':
                conn.execute("INSERT INTO settings VALUES ('result','published')")
    return write


@pytest.mark.parametrize('kind', OPERATIONS)
def test_contended_background_entry_point_leaves_foreground_available(database, kind, record_property):
    store = database
    attempted = threading.Event()
    store.conn.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
    original_timeout = store.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    with ExitStack() as stack:
        run = operation(store, kind, stack)
        blocker = stack.enter_context(closing(sqlite3.connect(store._db_path)))
        blocker.execute('BEGIN IMMEDIATE')
        def background():
            with store.priority.background(check_cancellable_work):
                return run()
        with ThreadPoolExecutor(1) as workers:
            pending = workers.submit(background)
            deadline = time.monotonic() + 2
            while not attempted.is_set() and not pending.done() and time.monotonic() < deadline:
                time.sleep(.001)
            latencies = []
            try:
                assert attempted.is_set() or pending.done()
                for _ in range(8):
                    started = time.monotonic()
                    with store._lock:
                        assert store.conn.execute('SELECT 1').fetchone()[0] == 1
                    latencies.append(time.monotonic() - started)
                record_property('foreground_seconds', latencies)
                assert max(latencies) < 1
            finally:
                blocker.rollback()
            result = pending.result(timeout=3)
        if kind == 'snapshot':
            assert result is True
    assert not store.conn.in_transaction
    assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == original_timeout
    if kind in ('snapshot', 'recommendation-write'):
        assert store.settings.get_setting('result') == 'published'
    if kind == 'provider-changed':
        assert store.conn.execute('SELECT fingerprint FROM enrichment_provider_config').fetchone()[0] == 'changed'


@pytest.mark.parametrize('kind', ['provider-changed', 'recommendation-write', 'snapshot'])
@pytest.mark.parametrize('ending', ['cancel', 'timeout'])
def test_contended_background_unit_cancels_or_times_out_without_partial_output(database, kind, ending):
    store = database
    stopped = threading.Event()
    if ending == 'cancel':
        store.conn.set_trace_callback(lambda sql: stopped.set() if sql == 'BEGIN IMMEDIATE' else None)
    with ExitStack() as stack:
        run = operation(store, kind, stack)
        with closing(sqlite3.connect(store._db_path)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with pytest.raises(WorkClosing if ending == 'cancel' else sqlite3.OperationalError):
                with cancellable_work(stopped.is_set), store.priority.background(check_cancellable_work):
                    run()
            assert time.monotonic() - started < (1 if ending == 'cancel' else 2)
        assert not store.conn.in_transaction
        assert store.settings.get_setting('result') is None
        assert store.conn.execute('SELECT fingerprint FROM enrichment_provider_config').fetchone()[0] == 'original'
        store.conn.set_trace_callback(None)
        stopped.clear()
        run()
    assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    if kind != 'provider-changed':
        assert store.settings.get_setting('result') == 'published'


def test_external_commit_while_publication_waits_still_rejects_stale_inputs(database):
    store = database
    attempted = threading.Event()
    with store.read_snapshot() as snapshot, closing(sqlite3.connect(store._db_path)) as blocker:
        assert snapshot.store.settings.get_setting('input') is None
        blocker.execute("INSERT INTO settings VALUES ('input','new')")
        store.conn.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
        with ThreadPoolExecutor(1) as workers:
            pending = workers.submit(snapshot.publish, lambda: pytest.fail('Published stale inputs'))
            try:
                assert attempted.wait(2)
            finally:
                blocker.commit()
            assert pending.result(timeout=3) is False
    assert store.settings.get_setting('input') == 'new'
    with store.read_snapshot() as retry:
        assert retry.store.settings.get_setting('input') == 'new'
        assert retry.publish(lambda: store.settings.publish_values({'result': 'fresh'}))
    assert store.settings.get_setting('result') == 'fresh'


def test_provider_configuration_and_refresh_frontier_rollback_together(database, monkeypatch):
    store = database
    start = provider_frontiers.start
    def failed(conn, *args):
        start(conn, *args)
        raise OSError('injected failure after configuration and frontier writes')
    with monkeypatch.context() as scope:
        scope.setattr(provider_frontiers, 'start', failed)
        with pytest.raises(OSError, match='injected failure'):
            store.enrichment_state.reconcile_provider_configuration(CHANGED)
    assert not store.conn.in_transaction
    assert store.conn.execute('SELECT fingerprint FROM enrichment_provider_config').fetchone()[0] == 'original'
    assert provider_frontiers.read(store.conn, 'deezer') is None
    store.enrichment_state.reconcile_provider_configuration(CHANGED)
    assert store.conn.execute('SELECT fingerprint FROM enrichment_provider_config').fetchone()[0] == 'changed'
