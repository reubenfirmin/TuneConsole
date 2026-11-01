"""Pending feedback and committed receipts survive independent library recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest

from yt_playlist.core import playback_feedback, playback_feedback_backup, policy_recovery
from yt_playlist.core.store import Store


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path / 'source.db')
    store.init_schema()
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    store.tracks.upsert_track('song', 'Song', 'Artist', None, 180)
    yield store, account
    store.conn.close()


def event(position):
    return {'kind': 'next', 'videoId': 'song', 'position': position, 'duration': 180}


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_recovery_keeps_pending_feedback_and_receipts_without_source_files(source, tmp_path, monkeypatch):
    store, account = source
    queue = playback_feedback.inbox(store)
    playback_feedback.capture(store, 'player', account, event(10), 100)
    acknowledge = queue.acknowledge

    def interrupted(_capture):
        raise OSError('Controlled interruption after main receipt commit')

    monkeypatch.setattr(queue, 'acknowledge', interrupted)
    with pytest.raises(OSError, match='after main receipt commit'):
        playback_feedback.drain(store)
    monkeypatch.setattr(queue, 'acknowledge', acknowledge)
    playback_feedback.capture(store, 'rating', account, {'video_id': 'song', 'status': 'LIKE'}, 101)
    playback_feedback.capture(store, 'player', account, event(30), 102)
    assert queue.pending() == 3
    assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1
    store.conn.close()
    original = digest(store._db_path), digest(queue.path)
    report = policy_recovery.recover(store._db_path, tmp_path / 'recovered',
                                    progress=lambda _: None)
    assert (digest(store._db_path), digest(queue.path)) == original
    assert 'playback_feedback' in report['verified_backup_file_manifests']
    restored = tmp_path / 'restored.db'
    with closing(readonly(report['verified_backup'])) as backup, closing(sqlite3.connect(restored)) as target:
        backup.backup(target)
        policy_recovery._restore_files(target, report['verified_backup_file_manifests'])
    shutil.rmtree(queue.directory)
    for database in (Path(report['compact_database']), restored):
        candidate = Store(database)
        try:
            candidate.init_schema()
            pending = playback_feedback.inbox(candidate)
            assert pending.pending() == 3
            assert Path(pending.next()['filename']).is_relative_to(pending.directory)
            assert playback_feedback.drain(candidate) == 3
            assert playback_feedback.drain(candidate) == 0
            assert candidate.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 2
            assert candidate.tracks.item_rating(account, 'song')['status'] == 'LIKE'
            assert candidate.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 3
        finally:
            candidate.conn.close()


def test_capture_and_replay_continue_after_backup_pins_main(source, tmp_path):
    store, account = source
    queue = playback_feedback.inbox(store)
    first = playback_feedback.capture(store, 'player', account, event(10), 100)
    destination = tmp_path / 'snapshot.db'
    with closing(readonly(store._db_path)) as reader:
        with playback_feedback_backup.pinned(reader, destination) as manifest:
            with ThreadPoolExecutor(1) as worker:
                # The large main backup has not started. Captures already have
                # their fence back, and replay may delete the source files.
                worker.submit(queue.capture, 'player', account, event(20), 101).result(timeout=2)
            assert playback_feedback.drain(store) == 2
            assert not queue.pending()
            assert not list(queue.directory.glob('*/*.json'))
            with closing(sqlite3.connect(destination)) as target:
                reader.backup(target)
                playback_feedback_backup.restore(target, manifest)
    candidate = Store(destination)
    try:
        candidate.init_schema()
        assert candidate.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 0
        assert playback_feedback.inbox(candidate).next()['capture_id'] == first
        assert playback_feedback.drain(candidate) == 1
        assert candidate.conn.execute('SELECT position FROM player_events').fetchone()[0] == 10
        assert playback_feedback.drain(candidate) == 0
    finally:
        candidate.conn.close()


def test_independent_inboxes_share_the_snapshot_fence(source, tmp_path, monkeypatch):
    store, account = source
    queue = playback_feedback.inbox(store)
    playback_feedback.capture(store, 'player', account, event(10), 100)
    independent = playback_feedback.Inbox(store._db_path, None)
    attempted = threading.Event()
    copy = playback_feedback_backup._copy

    def capture():
        attempted.set()
        return independent.capture('player', account, event(20), 101)

    with ThreadPoolExecutor(1) as worker:
        pending = None

        def copying(reader, target):
            nonlocal pending
            pending = worker.submit(capture)
            assert attempted.wait(1)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            return copy(reader, target)

        monkeypatch.setattr(playback_feedback_backup, '_copy', copying)
        with closing(readonly(store._db_path)) as reader:
            with playback_feedback_backup.pinned(reader, tmp_path / 'snapshot.db') as manifest:
                assert manifest is not None
                assert pending.result(timeout=2)
                assert queue.pending() == 2


def test_corrupt_pending_observation_prevents_a_successful_recovery(source, tmp_path):
    store, account = source
    playback_feedback.capture(store, 'player', account, event(10), 100)
    queue = playback_feedback.inbox(store)
    Path(queue.next()['filename']).write_bytes(b'corrupt')
    output = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='retained content'):
        policy_recovery.recover(store._db_path, output, progress=lambda _: None)
    assert not (output / 'report.json').exists()
    assert queue.pending() == 1
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 0
