"""Durable playlist ownership and atomic conditional remote publication (#379)."""
import sqlite3

import pytest

from yt_playlist.core.store import Store


def seed(store, remote='PL', identity=None):
    identity = identity or store.identities.upsert_identity('main', 'bridge', None, True)
    ids = [store.tracks.upsert_track(v, v.upper(), 'Artist', 'Album', 200, library=False)
           for v in ('a', 'b', 'c')]
    revision = store.playlists.playlist_revision(identity, remote)
    pid = store.playlists.publish_playlist_snapshot(identity, remote, 'Mix', ids[:2], 100,
                                                    expected_revision=revision)
    return identity, pid, ids


def test_revision_guard_is_shared_by_independent_connections(tmp_path):
    path = tmp_path / 'ownership.db'
    first, second = Store(path), Store(path)
    try:
        first.init_schema()
        iid, pid, ids = seed(first)
        second.init_schema()
        revision = first.playlists.playlist_revision(iid, 'PL')
        second.playlists.set_playlist_tracks(pid, ids)
        second.playlists.set_playlist_track_count(pid, 3, 210)
        changed = second.playlists.get_playlist(pid)
        assert first.playlists.publish_playlist_snapshot(
            iid, 'PL', 'Old title', ids[:2], 200, expected_revision=revision) is None
        assert first.playlists.get_playlist(pid) == changed
        assert first.playlists.get_playlist_track_ids(pid) == ids
    finally:
        first.conn.close()
        second.conn.close()


def test_metadata_and_order_publish_in_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / 'publication.db'
    store = Store(path)
    try:
        store.init_schema()
        iid, pid, ids = seed(store)
        revision = store.playlists.playlist_revision(iid, 'PL')
        original = store.playlists._set_playlist_tracks
        with sqlite3.connect(path) as reader:
            def observed(playlist_id, track_ids):
                # Even after metadata has been written, another connection sees the
                # complete previous publication, never a new title with old members.
                assert store.conn.in_transaction
                assert reader.execute('SELECT title,track_count,last_seen FROM playlists WHERE id=?',
                                      (pid,)).fetchone() == ('Mix', 2, 100)
                assert reader.execute('SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position',
                                      (pid,)).fetchall() == [('a',), ('b',)]
                original(playlist_id, track_ids)
            monkeypatch.setattr(store.playlists, '_set_playlist_tracks', observed)
            assert store.playlists.publish_playlist_snapshot(
                iid, 'PL', 'New title', [ids[2], ids[0]], 200, expected_revision=revision) == pid
            assert reader.execute('SELECT title,track_count,last_seen FROM playlists WHERE id=?',
                                  (pid,)).fetchone() == ('New title', 2, 200)
            assert reader.execute('SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position',
                                  (pid,)).fetchall() == [('c',), ('a',)]
    finally:
        store.conn.close()


def test_failed_publication_rolls_back_metadata_membership_and_revision(store):
    iid, pid, ids = seed(store)
    before = store.playlists.get_playlist(pid)
    revision = store.playlists.playlist_revision(iid, 'PL')
    with store.conn:
        store.conn.execute("CREATE TEMP TRIGGER reject_snapshot BEFORE INSERT ON playlist_items "
                           "WHEN NEW.video_id='c' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        store.playlists.publish_playlist_snapshot(iid, 'PL', 'New title', ids, 200,
                                                  expected_revision=revision)
    assert store.playlists.get_playlist(pid) == before
    assert store.playlists.get_playlist_track_ids(pid) == ids[:2]
    assert store.playlists.playlist_revision(iid, 'PL') == revision
    assert not store.conn.execute('SELECT 1 FROM library_memberships WHERE entry_id=?', (ids[2],)).fetchone()


def test_tokens_survive_removal_and_reappearance_including_initial_absence(store):
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    absent = store.playlists.playlist_revision(iid, 'PL')
    _, pid, ids = seed(store, identity=iid)
    store.playlists.remove_playlist(pid)
    assert store.playlists.publish_playlist_snapshot(iid, 'PL', 'Stale', ids, 200,
                                                    expected_revision=absent) is None
    removed = store.playlists.playlist_revision(iid, 'PL')
    assert removed > absent
    replacement = store.playlists.publish_playlist_snapshot(iid, 'PL', 'Reappeared', ids, 300,
                                                            expected_revision=removed)
    assert replacement != pid
    assert store.playlists.get_playlist(replacement).title == 'Reappeared'


@pytest.mark.parametrize('remote', ['PL', 'LM'])
def test_another_account_or_playlist_does_not_invalidate_this_owner(store, remote):
    iid, pid, ids = seed(store, remote)
    other = store.identities.upsert_identity('other', 'bridge', 'other', False)
    seed(store, remote, other)
    revision = store.playlists.playlist_revision(iid, remote)
    other_pid = store.playlists.upsert_playlist(other, remote, 'Other account', 0, '', 200)
    store.playlists.set_playlist_tracks(other_pid, [])
    store.playlists.upsert_playlist(iid, 'UNRELATED', 'Other playlist', 0, '', 200)
    assert store.playlists.publish_playlist_snapshot(iid, remote, 'Mine', ids, 300,
                                                    expected_revision=revision) == pid


def test_storage_and_compatibility_writers_advance_the_same_owner(store):
    iid, pid, ids = seed(store)
    unresolved = store.tracks.upsert_track(None, 'Unresolved', 'Artist', 'Album', 200)
    operations = [
        ('INSERT INTO playlist_tracks VALUES (?,?,?)', (pid, ids[2], 2)),
        ('UPDATE playlist_items SET position=5 WHERE playlist_id=? AND video_id=?', (pid, 'c')),
        ('DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?', (pid, ids[2])),
        ('INSERT INTO playlist_tracks VALUES (?,?,?)', (pid, unresolved, 2)),
        ('UPDATE unresolved_playlist_items SET position=5 WHERE playlist_id=?', (pid,)),
        ('DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?', (pid, unresolved)),
    ]
    for sql, args in operations:
        revision = store.playlists.playlist_revision(iid, 'PL')
        with store.conn:
            store.conn.execute(sql, args)
        assert store.playlists.playlist_revision(iid, 'PL') > revision
