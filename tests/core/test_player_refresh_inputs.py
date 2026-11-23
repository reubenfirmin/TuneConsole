"""Check how player events affect ranking refreshes and skip evidence snapshots."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import validate_version
from tests.core.test_snapshot_revisions import database


@pytest.fixture
def player_store(database):
    database.accounts.upsert_account('Playback fixture', None, True)
    return database


def event(store, kind):
    identity = store.accounts.get_accounts()[0].id
    return store.player_events.record_player_event(identity, kind, 'video', 10, 200, None, '{}', 100)


@pytest.mark.parametrize('kind', ['tick', 'rate', 'pause', 'track_exit', 'bye', 'ended'])
def test_only_ranking_events_queue_a_refresh(player_store, kind):
    store = player_store
    store.rec_refresh.acknowledge(store.rec_refresh.revision())
    event(store, kind)
    assert (store.rec_refresh.revision() is not None) is (kind in ('track_exit', 'bye', 'ended'))


@pytest.mark.parametrize('kind', ['tick', 'rate', 'track_exit', 'bye'])
def test_skip_snapshot_tracks_consumed_event_kinds(player_store, kind):
    store = player_store
    with store.read_snapshot() as snapshot:
        snapshot.store.player_events.recent_skips_with_ts(0)
        event(store, kind)
        assert snapshot.publish(lambda: None) is (kind not in ('track_exit', 'bye'))


def test_reclassifying_or_deleting_a_consumed_event_invalidates_snapshot(player_store):
    store = player_store
    row = event(store, 'tick')
    for sql in ("UPDATE player_events SET kind='track_exit' WHERE id=?",
                "UPDATE player_events SET kind='tick' WHERE id=?"):
        with store.read_snapshot() as snapshot:
            snapshot.store.player_events.recent_skips_with_ts(0)
            store.conn.execute(sql, (row,))
            store.conn.commit()
            assert not snapshot.publish(lambda: None)
    row = event(store, 'bye')
    with store.read_snapshot() as snapshot:
        snapshot.store.player_events.recent_skips_with_ts(0)
        store.conn.execute('DELETE FROM player_events WHERE id=?', (row,))
        store.conn.commit()
        assert not snapshot.publish(lambda: None)
