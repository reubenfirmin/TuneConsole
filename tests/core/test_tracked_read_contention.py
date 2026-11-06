"""Cache revision bookkeeping cannot put committed reads behind the writer."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from yt_playlist.core import db_reads
from yt_playlist.core.db_snapshot import SnapshotBusy


@contextmanager
def _held_writer(store):
    entered, release = threading.Event(), threading.Event()

    def write():
        with store._lock:
            store.conn.execute('BEGIN')
            try:
                store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='input'")
                entered.set()
                assert release.wait(5), 'Read waited for the held writer'
            finally:
                store.conn.rollback()

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        try:
            assert entered.wait(2), 'Writer did not start'
            yield
            assert not release.is_set()
            assert not writer.done(), 'Read only completed after the writer exited'
        finally:
            release.set()
            writer.result(timeout=2)


def _read(reader, calls):
    def calculate():
        calls.append(True)
        return {'value': reader.settings.get_setting('input')}
    return reader.memoized_read('contention-input', calculate)


def _assert_released(store):
    acquired = []
    try:
        for _ in range(db_reads.MAX_READERS):
            assert store._analytics_readers.acquire(blocking=False)
            acquired.append(True)
        assert not store._analytics_readers.acquire(blocking=False)
    finally:
        for _ in acquired:
            store._analytics_readers.release()
    assert not store._reader_diagnostics.owners


def test_tracked_view_reads_committed_data_while_writer_is_held(file_backed_store):
    store, calls = file_backed_store, []
    store.settings.set_setting('input', 'before')
    with store.read_view(tracked=True) as reader:
        assert _read(reader, calls) == {'value': 'before'}
    store.settings.set_setting('input', 'committed')
    with _held_writer(store):
        with store.read_view(tracked=True) as reader:
            result = _read(reader, calls)
            assert result == {'value': 'committed'}
            result['value'] = 'caller mutation'
            assert _read(reader, calls) == {'value': 'committed'}
        assert len(calls) == 2
    _assert_released(store)
    with store.read_view(tracked=True) as reader:
        assert _read(reader, calls) == {'value': 'committed'}


def test_cache_validation_recalculates_without_waiting_for_writer(file_backed_store):
    store, calls = file_backed_store, []
    store.settings.set_setting('input', 'before')
    with store.read_view(tracked=True) as reader:
        assert _read(reader, calls) == {'value': 'before'}
    # Force validation against a later serial, rather than the equal-serial hit.
    store.settings.set_setting('unrelated', 'new')
    with store.read_view(tracked=True) as reader:
        with _held_writer(store):
            assert _read(reader, calls) == {'value': 'before'}
            assert len(calls) == 2
    _assert_released(store)


@pytest.mark.parametrize('busy', [False, True])
def test_reader_failure_is_propagated_once_and_releases_capacity(file_backed_store, busy):
    store = file_backed_store
    store.settings.set_setting('input', 'committed')

    def fail():
        with pytest.raises(SnapshotBusy, match='caller failure'):
            with store.read_view(tracked=True) as reader:
                assert reader.settings.get_setting('input') == 'committed'
                raise SnapshotBusy('caller failure')

    if busy:
        with _held_writer(store):
            fail()
    else:
        fail()
    _assert_released(store)
