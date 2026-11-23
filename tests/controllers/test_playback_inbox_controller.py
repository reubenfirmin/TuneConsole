from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao
from yt_playlist.library.playback_inbox_composition import inbox_for_database

pytestmark = pytest.mark.usefixtures('no_database')


def test_capture_and_acknowledgement_publish_whole_generations(tmp_path, monkeypatch):
    database = tmp_path / 'state.db'
    queue = inbox_for_database(database, 'private')
    first = queue.capture('player', 7, {'videoId': 'first'}, 100)
    original = fao.read(queue.path).bytes

    def interrupted(*args, **kwargs):
        raise OSError('interrupted publication')

    with monkeypatch.context() as patch:
        patch.setattr(fao._PreparedFile, 'publish', interrupted)
        with pytest.raises(OSError, match='interrupted publication'):
            queue.capture('player', 7, {'videoId': 'second'}, 101)
        with pytest.raises(OSError, match='interrupted publication'):
            queue.acknowledge(first)
    assert fao.read(queue.path).bytes == original
    reopened = inbox_for_database(database, 'reopened')
    assert reopened.pending() == 1 and reopened.next()['capture_id'] == first
    reopened.acknowledge(first)
    assert reopened.pending() == 0


def test_independent_queue_bindings_preserve_all_concurrent_captures(tmp_path):
    database = tmp_path / 'state.db'
    queues = [inbox_for_database(database, 'private') for _ in range(4)]

    def capture(index):
        return queues[index % len(queues)].capture('player', 7, {'videoId': str(index)}, 100 + index)

    with ThreadPoolExecutor(max_workers=4) as workers:
        expected = set(workers.map(capture, range(16)))
    assert queues[0].pending() == len(expected) == 16
    for sequence in range(1, 17):
        row = queues[0].next()
        assert row['sequence'] == sequence
        expected.remove(row['capture_id'])
        queues[1].acknowledge(row['capture_id'])
        queues[2].acknowledge(row['capture_id'])
    assert not expected and queues[3].pending() == 0
