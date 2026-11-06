"""Foreground reads remain possible while cooperative workers retain snapshots."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import threading

import pytest

from yt_playlist.core import activity, db_reads
from yt_playlist.core.store import Store


def test_background_snapshots_leave_capacity_for_foreground(file_backed_store):
    store = file_backed_store
    store.settings.set_setting('reader-priority-probe', 'old')
    entered = [threading.Event() for _ in range(db_reads.MAX_READERS)]
    release = threading.Event()

    def background(index):
        with store.priority.background(activity.check_stopping), store.read_view() as reader:
            value = reader.settings.get_setting('reader-priority-probe')
            entered[index].set()
            assert release.wait(3), 'Foreground read could not finish while background readers were held'
            # Keep real WAL snapshot isolation while the foreground writes.
            assert reader.settings.get_setting('reader-priority-probe') == value
            return value

    with ThreadPoolExecutor(db_reads.MAX_READERS) as pool:
        pending = [pool.submit(background, n) for n in range(db_reads.MAX_READERS - 1)]
        try:
            for event in entered[:-1]:
                assert event.wait(2)
            pending.append(pool.submit(background, db_reads.MAX_READERS - 1))
            # The old admission policy lets the fourth worker occupy the final
            # slot. A reservation must leave it queued until a snapshot closes.
            entered[-1].wait(.1)
            store.settings.set_setting('reader-priority-probe', 'new')
            with activity.work_budget(.3), store.read_view() as reader:
                assert reader.settings.get_setting('reader-priority-probe') == 'new'
            assert not entered[-1].is_set()
        finally:
            release.set()
        assert [future.result(3) for future in pending[:-1]] == ['old'] * (db_reads.MAX_READERS - 1)
        # The queued worker opens after the write and must see the new commit.
        assert pending[-1].result(3) == 'new'


def _assert_capacity_released(store):
    for gate, count in ((store._analytics_readers, db_reads.MAX_READERS),
                        (store._background_readers, db_reads.MAX_READERS - 1)):
        acquired = []
        try:
            for _ in range(count):
                assert gate.acquire(blocking=False)
                acquired.append(gate)
            assert not gate.acquire(blocking=False)
        finally:
            for held in acquired:
                held.release()
    assert not store._reader_diagnostics.owners


def test_background_wait_for_full_pool_is_bounded_and_releases_quota(file_backed_store):
    store = file_backed_store
    with ExitStack() as held:
        for _ in range(db_reads.MAX_READERS):
            held.enter_context(store.read_view())
        # The complete budget checks its deadline again on exit. Keep that
        # boundary inside the expected failure as well as reader admission.
        with pytest.raises(activity.WorkBudgetExceeded), \
                store.priority.background(activity.check_stopping), \
                activity.work_budget(.05), store.read_view():
            pytest.fail('Background read bypassed the full global limit')
    _assert_capacity_released(store)


def test_failed_background_reader_setup_releases_both_limits(file_backed_store, monkeypatch):
    store = file_backed_store

    def fail(reader):
        raise ValueError('reader setup failed')

    monkeypatch.setattr(Store, '_bind_repositories', fail)
    with store.priority.background(activity.check_stopping):
        with pytest.raises(ValueError, match='reader setup failed'), store.read_view():
            pytest.fail('Failed reader setup was exposed')
    _assert_capacity_released(store)
