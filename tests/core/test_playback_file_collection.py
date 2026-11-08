"""Observation collection respects pending captures and file-before-SQL custody."""
from yt_playlist.core import file_collection
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from yt_playlist.core import fao, playback_feedback
from yt_playlist.core.preparation import managed_files
from tests.playback_feedback_support import event
from tests.store_fixture_support import bounded_consumer_schema


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Inbox collection tests do not construct the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def close_collection(file_backed_store):
    yield
    managed_files.close(file_backed_store.retention.database)


def collect(store, now):
    database = store.retention.database
    completed = set()
    for _ in range(128):
        work = getattr(database, '_managed_file_orphans', None)
        position = work.position if work is not None else 0
        store.retention.maintain_managed_files(now)
        work = database._managed_file_orphans
        if work.position != position:
            completed.add(position)
        # A zero-removal turn does not prove traversal. Visit every ownership
        # domain and finish the crash-left stream scan before checking custody.
        if len(completed) == len(work.contracts) and not hasattr(database, '_managed_file_pending'):
            return
    pytest.fail('Managed-file collection did not complete within 128 turns')


@pytest.mark.parametrize('failure', ['insert', 'acknowledge'])
def test_observation_orphans_collect_while_pending_capture_replays_once(file_backed_store, monkeypatch, failure):
    store = file_backed_store
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    store.tracks.upsert_track('song', 'Song', 'Artist', None, 180)
    queue = playback_feedback.inbox(store)
    queue.capture('player', account, event(10), 100)
    if failure == 'insert':
        before = set((p for p in file_collection.walk_entries(queue.directory) if p is not None))
        with closing(queue.connect()) as conn, conn:
            conn.execute("CREATE TRIGGER fail_capture BEFORE INSERT ON playback_feedback_inbox "
                "WHEN NEW.observed_at=101 BEGIN SELECT RAISE(ABORT,'controlled insert failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled insert failure'):
            queue.capture('player', account, event(99), 101)
        orphan, = set((p for p in file_collection.walk_entries(queue.directory) if p is not None)) - before
        expected = 0
    else:
        orphan = Path(queue.next()['filename'])
        delete = fao.delete
        def interrupted(path, **kwargs):
            if Path(path) == orphan:
                raise OSError('controlled post-ack failure')
            return delete(path, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(fao, 'delete', interrupted)
            with pytest.raises(OSError, match='controlled post-ack failure'):
                playback_feedback.drain(store)
        queue.capture('player', account, event(20), 102)
        expected = 1
    pending = queue.next()
    encoded = fao.read(pending['filename']).bytes
    abandoned = queue.directory / '.fao-pending-abandoned'
    abandoned.write_bytes(b'incomplete private stream')
    collect(store, time.time())
    assert orphan.exists() and abandoned.exists()
    managed_files.close(store.retention.database)
    collect(store, time.time() + 31 * 86400)
    assert not orphan.exists() and not abandoned.exists()
    assert fao.read(pending['filename']).bytes == encoded
    assert queue.pending() == 1
    assert playback_feedback.drain(store) == 1
    assert playback_feedback.drain(store) == 0
    assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == expected + 1
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == expected + 1


@pytest.mark.parametrize('rollback', [False, True])
def test_collection_cannot_remove_active_capture_before_inbox_insert(file_backed_store, monkeypatch, rollback):
    store = file_backed_store
    queue = playback_feedback.inbox(store)
    entered, release, paths = threading.Event(), threading.Event(), []
    publish = queue._publish
    def held_publish(*args):
        owner = publish(*args)
        paths.append(Path(owner[0]))
        entered.set()
        assert release.wait(15)
        if rollback:
            raise RuntimeError('controlled capture rollback')
        return owner
    monkeypatch.setattr(queue, '_publish', held_publish)
    with ThreadPoolExecutor(1) as executor:
        capture = executor.submit(queue.capture, 'player', 1, event(10), 100)
        try:
            assert entered.wait(5)
            assert queue.pending() == 0
            collect(store, time.time() + 31 * 86400)
            assert paths[0].exists()
            with file_collection.collector(queue.directory) as admitted:
                assert not admitted
        finally:
            release.set()
        if rollback:
            with pytest.raises(RuntimeError, match='controlled capture rollback'):
                capture.result(timeout=10)
        else:
            capture.result(timeout=10)
    managed_files.close(store.retention.database)
    collect(store, time.time() + 31 * 86400)
    assert paths[0].exists() is not rollback
