"""Playback retention visits abandoned FAO stages and retired predecessor files."""
import time
from contextlib import closing

import pytest

from tests.playback_feedback_support import event
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core import fao, playback_feedback, playback_inbox_upgrade
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


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
        if len(completed) == len(work.contracts) and not hasattr(database, '_managed_file_pending'):
            return
    pytest.fail('Managed-file collection did not complete within 128 turns')


def test_collection_keeps_queue_and_expires_abandoned_and_retired_files(tmp_path, _pristine_current_schema):
    database = tmp_path / 'state.db'
    previous_inbox = playback_inbox_upgrade.legacy_path(database)
    identity = 'a' * 64
    retired = playback_inbox_upgrade.observation_root(previous_inbox) / identity[:2] / (identity + '.json')
    fao.write(retired, b'{"format":1}')
    store = Store(database)
    with closing(store.conn):
        initialize_consumer(store, _pristine_current_schema)
        account = store.accounts.upsert_account('Main', None, True)
        assert account == 1
        store.tracks.upsert_track('song', 'Song', 'Artist', None, 180)
        queue = playback_feedback.inbox(store)
        queue.capture('player', account, event(10), 100)
        queue.capture('player', account, event(20), 101)
        original = fao.read(queue.path).bytes
        abandoned = queue.path.parent / '.fao-pending-abandoned'
        abandoned.write_bytes(b'incomplete private stream')
        try:
            collect(store, time.time())
            assert retired.exists() and abandoned.exists()
            store.retention.managed.close()
            collect(store, time.time() + 31 * 86400)
            assert not retired.exists() and not abandoned.exists()
            assert fao.read(queue.path).bytes == original
            assert playback_feedback.drain(store) == 2
            assert playback_feedback.drain(store) == 0
            assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 2
        finally:
            store.retention.managed.close()
