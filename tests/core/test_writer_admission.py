"""SQLite contention must release ServingLock and retain transaction ownership."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import cancellable_work, WorkClosing
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.priority import RequestPriority, ServingLock


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'writes.db'
    with closing(sqlite3.connect(path, check_same_thread=False, factory=DiagnosticConnection)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO settings VALUES ('input','old')")
        conn.commit()
        yield SimpleNamespace(conn=conn, _lock=ServingLock(RequestPriority()), _db_path=path)


def test_waiting_writer_leaves_serving_reads_available(store):
    attempted = threading.Event()
    store.conn.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
    original_timeout = store.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    with closing(sqlite3.connect(store._db_path)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        def write():
            with atomic_write(store):
                store.conn.execute("UPDATE settings SET value='new' WHERE key='input'")
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(write)
            try:
                assert attempted.wait(2)
                started = time.monotonic()
                assert store._lock.acquire(timeout=.2)
                try:
                    assert store.conn.execute('SELECT value FROM settings').fetchone()[0] == 'old'
                finally:
                    store._lock.release()
                assert time.monotonic() - started < .3
                assert not pending.done()
            finally:
                blocker.rollback()
            pending.result(timeout=2)
    assert store.conn.execute('SELECT value FROM settings').fetchone()[0] == 'new'
    assert not store.conn.in_transaction
    assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == original_timeout


@pytest.mark.parametrize('ending', ['timeout', 'cancel', 'outer-lock'])
def test_failed_admission_is_bounded_and_restores_connection(store, ending):
    cancelled = threading.Event()
    if ending == 'cancel':
        store.conn.set_trace_callback(lambda sql: cancelled.set() if sql == 'BEGIN IMMEDIATE' else None)
    original_timeout = store.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    with closing(sqlite3.connect(store._db_path)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        if ending == 'outer-lock':
            store._lock.acquire()
        started = time.monotonic()
        try:
            with pytest.raises(WorkClosing if ending == 'cancel' else sqlite3.OperationalError):
                with cancellable_work(cancelled.is_set), atomic_write(store, wait_seconds=.05):
                    pytest.fail('Admitted a second database writer')
        finally:
            if ending == 'outer-lock':
                store._lock.release()
        assert time.monotonic() - started < .5
    assert not store.conn.in_transaction
    assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == original_timeout
    with atomic_write(store):
        store.conn.execute("UPDATE settings SET value='retry'")
    assert store.conn.execute('SELECT value FROM settings').fetchone()[0] == 'retry'


def test_joined_write_failure_and_success_leave_prior_caller_edits_pending(store):
    store.conn.execute('BEGIN IMMEDIATE')
    store.conn.execute("INSERT INTO settings VALUES ('caller','draft')")
    with pytest.raises(ValueError, match='failed mutation'):
        with atomic_write(store):
            store.conn.execute("UPDATE settings SET value='failed' WHERE key='input'")
            raise ValueError('failed mutation')
    assert store.conn.in_transaction
    assert dict(store.conn.execute('SELECT * FROM settings')) == {'input': 'old', 'caller': 'draft'}
    with atomic_write(store):
        store.conn.execute("UPDATE settings SET value='pending' WHERE key='input'")
        store.conn.commit()
    assert store.conn.in_transaction
    with closing(sqlite3.connect(store._db_path)) as observer:
        assert dict(observer.execute('SELECT * FROM settings')) == {'input': 'old'}
    store.conn.rollback()
    assert dict(store.conn.execute('SELECT * FROM settings')) == {'input': 'old'}
