"""Current playback queue operation uses file storage."""
import sqlite3

import pytest

from yt_playlist.library.playback_inbox_composition import inbox_for_database


def test_fresh_queue_and_normal_queue_operations_open_no_sqlite_connection(
        tmp_path, monkeypatch, consumer_store):
    database = tmp_path / 'state.db'
    consumer_store(database)

    def unexpected(*args, **kwargs):
        pytest.fail('Playback queue opened SQLite')

    monkeypatch.setattr(sqlite3, 'connect', unexpected)
    queue = inbox_for_database(database, 'private')
    capture = queue.capture('rating', 1, {'video_id': 'song', 'status': 'LIKE'}, 100)
    assert queue.pending() == 1
    assert queue.rating(1, 'song')['status'] == 'LIKE'
    assert queue.next()['capture_id'] == capture
    queue.acknowledge(capture)
    assert queue.next() is None
    assert list(tmp_path.glob('*.sqlite3*')) == []
