"""Verify the connection ownership guaranteed by the lookup_read boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import sqlite3
import threading

import pytest

from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.core.store import Store
from yt_playlist.repos.base import Repo, lookup_read


def assert_lock_owned(lock, expected):
    # Both lock types are reentrant. Acquisition by this thread plus exclusion
    # of a different thread proves ownership without a lock-specific property.
    assert lock.acquire(blocking=False)
    lock.release()

    def available():
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(available).result(timeout=2) is (not expected)


class LookupProbe(Repo):
    @lookup_read
    def value(self, observed, failure=None):
        assert_lock_owned(self._lock, True)
        observed.append(self._store)
        value = self.conn.execute("SELECT value FROM settings WHERE key='probe'").fetchone()[0]
        if failure is not None:
            raise failure('controlled method failure')
        return value


@pytest.mark.parametrize('route', ['memory', 'available', 'owned', 'private'])
@pytest.mark.parametrize('failure', [None, ValueError, WorkClosing])
def test_lookup_owns_its_connection_and_releases_on_every_exit(tmp_path, route, failure):
    store = Store(':memory:' if route == 'memory' else tmp_path / 'lookup.db')
    store.init_schema()
    store.settings.set_setting('probe', 'committed')
    observed = []
    try:
        with ExitStack() as stack:
            owner = store
            if route == 'private':
                owner = stack.enter_context(store.read_view())
            if route == 'owned':
                stack.enter_context(store._lock)
                store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='probe'")
                stack.callback(store.conn.rollback)
            if failure is None:
                assert LookupProbe(owner).value(observed) == (
                    'uncommitted' if route == 'owned' else 'committed')
            else:
                with pytest.raises(failure, match='controlled method failure'):
                    LookupProbe(owner).value(observed, failure)
            assert observed == [owner]
            assert_lock_owned(owner._lock, route == 'owned')
        assert_lock_owned(store._lock, False)
        if route == 'private':
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                owner.conn.execute('SELECT 1')
        assert store.settings.get_setting('probe') == 'committed'
        with pytest.raises(WorkClosing), cancellable_work(lambda: True):
            LookupProbe(store).value(observed)
        assert len(observed) == 1, 'Cancellation must happen before entering the method'
        assert_lock_owned(store._lock, False)
    finally:
        store.conn.close()


@pytest.mark.parametrize('failure', [None, ValueError, WorkClosing])
def test_busy_lookup_owns_private_connection_and_closes_it_on_every_exit(tmp_path, failure):
    store = Store(tmp_path / 'busy.db')
    store.init_schema()
    store.settings.set_setting('probe', 'committed')
    entered, release = threading.Event(), threading.Event()
    observed = []

    def writer():
        with store._lock:
            store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='probe'")
            try:
                entered.set()
                assert release.wait(5), 'Lookup waited for the serving connection'
            finally:
                store.conn.rollback()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(writer)
            try:
                assert entered.wait(2)
                if failure is None:
                    assert LookupProbe(store).value(observed) == 'committed'
                else:
                    with pytest.raises(failure, match='controlled method failure'):
                        LookupProbe(store).value(observed, failure)
                assert not pending.done(), 'The private read must finish while publication is active'
                assert len(observed) == 1
                reader = observed[0]
                assert reader is not store and reader._read_only
                assert_lock_owned(reader._lock, False)
                with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                    reader.conn.execute('SELECT 1')
            finally:
                release.set()
            pending.result(timeout=2)
        assert_lock_owned(store._lock, False)
        assert store.settings.get_setting('probe') == 'committed'
    finally:
        store.conn.close()
