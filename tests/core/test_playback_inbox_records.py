"""Private SQL/file owners for interruption, restart and installed-format admission."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core import fao
from yt_playlist.core.playback_observation_files import encode
from yt_playlist.library.playback_inbox_composition import inbox_for_database
from yt_playlist.repos.playback_inbox import SCHEMA


@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interrupted_capture_keeps_its_database_identity_and_recovers_verified_bytes_after_restart(tmp_path, monkeypatch, failure):
    database = tmp_path / 'state.db'
    queue = inbox_for_database(database, 'private')
    target, method = (queue.files, 'publish') if failure == 'file' else (queue.repository, 'complete')

    def interrupted(*args):
        raise OSError('interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(target, method, interrupted)
        with pytest.raises(OSError, match='interrupted'):
            queue.capture('player', 7, {'videoId': 'first'}, 100)
    with closing(sqlite3.connect(queue.path)) as conn:
        conn.row_factory = sqlite3.Row
        incomplete = dict(conn.execute('SELECT * FROM playback_feedback_inbox').fetchone())
    assert incomplete['file_status'] == 'pending'
    assert Path(incomplete['filename']).stem == incomplete['capture_id']
    assert incomplete['capture_id'] != incomplete['content_digest']
    assert Path(incomplete['filename']).exists() is (failure == 'status')
    assert queue.pending() == 0

    reopened = inbox_for_database(database, 'another-session')
    later = reopened.capture('player', 7, {'videoId': 'second'}, 101)
    if failure == 'status':
        first = reopened.next()
        assert first['capture_id'] == incomplete['capture_id'] and first['file_status'] == 'ready'
        assert reopened.body(first) == {'videoId': 'first'}
        reopened.acknowledge(first['capture_id'])
    row = reopened.next()
    assert row['capture_id'] == later and reopened.body(row) == {'videoId': 'second'}
    reopened.acknowledge(later)
    assert reopened.next() is None and reopened.pending() == 0


def test_hash_named_installed_capture_remains_readable_while_new_captures_use_database_ids(tmp_path):
    queue = inbox_for_database(tmp_path / 'state.db', 'private')
    capture = 'a' * 32
    encoded = encode(capture, 'player', {'videoId': 'installed'})
    digest = hashlib.sha256(encoded).hexdigest()
    filename = queue.directory / digest[:2] / (digest + '.json')
    fao.write(filename, encoded)
    original = filename.read_bytes()
    with closing(sqlite3.connect(queue.path)) as conn, conn:
        conn.execute(SCHEMA.replace(",\n    file_status TEXT NOT NULL DEFAULT 'ready' CHECK(file_status IN ('pending','ready'))", ''))
        conn.execute('INSERT INTO playback_feedback_inbox VALUES (1,?,?,?,?,?,?,?,?,?)',
            (capture, 'player', 7, 100, 'installed', None, str(filename), digest, len(encoded)))
    row = queue.next()
    assert row['capture_id'] == capture and queue.body(row) == {'videoId': 'installed'}
    later = queue.capture('player', 7, {'videoId': 'native'}, 101)
    assert filename.read_bytes() == original
    queue.acknowledge(capture)
    row = queue.next()
    assert Path(row['filename']).stem == later and queue.body(row) == {'videoId': 'native'}
