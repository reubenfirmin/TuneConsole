"""Telemetry does not schedule models or invalidate snapshots of skip evidence."""
import sqlite3

import pytest

from yt_playlist.core.migrations import current, migrate, registry
from tests.core.test_snapshot_revisions import database


@pytest.fixture
def player_store(database):
    database.identities.upsert_identity('Telemetry fixture', 'fixture', None, True)
    return database


def event(store, kind):
    identity = store.identities.get_identities()[0].id
    return store.player_events.record_player_event(identity, kind, 'video', 10, 200, None, '{}', 100)


@pytest.mark.parametrize('kind', ['tick', 'rate', 'pause', 'track_exit', 'bye', 'ended'])
def test_only_ranking_events_queue_a_refresh(player_store, kind):
    store = player_store
    store.conn.execute('DELETE FROM rec_change_queue')
    store.conn.commit()
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


def test_version_39_upgrade_preserves_events_and_pending_work(tmp_path):
    with sqlite3.connect(tmp_path / 'old.db') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn, registry=registry.MIGRATIONS[:39])
        conn.execute("INSERT INTO identities(id,label,credential_ref) VALUES (1,'Fixture','fixture')")
        conn.execute("INSERT INTO player_events(identity_id,kind,video_id,at) VALUES (1,'tick','video',100)")
        conn.commit()
        before = [tuple(row) for row in conn.execute('SELECT * FROM player_events')]
        pending = [tuple(row) for row in conn.execute('SELECT * FROM rec_change_queue')]
        migrate(conn)
        current.validate(conn)
        assert [tuple(row) for row in conn.execute('SELECT * FROM player_events')] == before
        assert [tuple(row) for row in conn.execute('SELECT * FROM rec_change_queue')] == pending
        conn.execute('DELETE FROM rec_change_queue')
        conn.execute("INSERT INTO player_events(identity_id,kind,video_id,at) VALUES (1,'tick','video',130)")
        assert conn.execute('SELECT COUNT(*) FROM rec_change_queue').fetchone()[0] == 0
        conn.execute("INSERT INTO player_events(identity_id,kind,video_id,at) VALUES (1,'track_exit','video',140)")
        assert conn.execute('SELECT COUNT(*) FROM rec_change_queue').fetchone()[0] == 1
