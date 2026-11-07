import sqlite3

import pytest

from yt_playlist.library.playlist_metadata import content_hash
from yt_playlist.util.matching import identity_key


def _track(i, **extra):
    return {"video_id": f"v{i}", "title": f"Song {i}", "artist": f"Artist {i}", "album": "Album", **extra}


def _playlist(store):
    identity = store.accounts.upsert_account("main", "ref", None, True)
    pid = store.playlists.upsert_playlist(identity, "RADIO", "Radio", 0, "", 1)
    store.playlists.mark_generated_playlist("RADIO")
    return pid


def _videos(store, pid):
    return [r[0] for r in store.conn.execute(
        "SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (pid,))]


def _library_videos(store):
    return {row[0] for row in store.conn.execute(
        'SELECT e.video_id FROM library_memberships m JOIN catalog_entries e ON e.id=m.entry_id')}


def _existing_prefix(store, pid, tracks):
    """Build the large precondition directly; the small case tests initial publication.

    Catalog insertion and multi-batch rollback have separate real owners below.
    This case measures appending one confirmed upload to an existing sequence.
    """
    rows = [(index + 1, identity_key(track['title'], track['artist']), track)
            for index, track in enumerate(tracks)]
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            [(rid, key, track['title'], track['artist']) for rid, key, track in rows])
        store.conn.executemany('INSERT INTO youtube_items(video_id,recording_id,title,artist,album) '
            'VALUES (?,?,?,?,?)', [(track['video_id'], rid, track['title'], track['artist'], track['album'])
                                  for rid, _, track in rows])
        store.conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
            [(rid, rid, track['video_id']) for rid, _, track in rows])
        store.conn.executemany("INSERT INTO library_memberships VALUES (?,'observed',2)",
            [(rid,) for rid, _, _ in rows])
        store.conn.executemany('INSERT INTO playlist_items VALUES (?,?,?)',
            [(pid, track['video_id'], index) for index, track in enumerate(tracks)])
        store.conn.execute('UPDATE playlists SET track_count=?,content_hash=?,last_seen=2 WHERE id=?',
            (len(tracks), content_hash([key for _, key, _ in rows]), pid))


@pytest.mark.parametrize("size", [8, 1024])
def test_append_writes_only_new_catalog_and_membership(store, monkeypatch, size):
    pid = _playlist(store)
    tracks = [_track(i) for i in range(size)]
    if size == 8:
        store.playlists.publish_confirmed_tracks(pid, "Radio", tracks, 2)
    else:
        _existing_prefix(store, pid, tracks)
    with store.conn:
        store.conn.execute("CREATE TEMP TABLE publication_writes(kind TEXT)")
        for operation in ["INSERT", "DELETE", "UPDATE"]:
            store.conn.execute(f"CREATE TEMP TRIGGER count_playlist_{operation} AFTER {operation} ON playlist_items "
                               f"BEGIN INSERT INTO publication_writes VALUES ('{operation}'); END")
    original, calls = store.tracks.upsert_track, []
    def observed(video, *args, **kwargs):
        calls.append(video)
        return original(video, *args, **kwargs)
    monkeypatch.setattr(store.tracks, "upsert_track", observed)
    expected = [t["video_id"] for t in tracks]
    tracks.append(_track(size))
    store.playlists.publish_confirmed_tracks(pid, "Radio", tracks, 3, expected_prefix=expected)
    assert calls == [f"v{size}"]
    assert [r[0] for r in store.conn.execute("SELECT kind FROM publication_writes")] == ["INSERT"]
    assert _videos(store, pid) == [t["video_id"] for t in tracks]
    playlist = store.playlists.get_playlist(pid)
    assert playlist.track_count == size + 1
    assert playlist.content_hash == content_hash(store.playlists.get_playlist_track_keys(pid))
    assert store.playlists.is_quarantined_playlist("RADIO")


@pytest.mark.parametrize("incremental", [False, True])
def test_confirmed_publication_rolls_back_all_changes(store, monkeypatch, incremental):
    """Reject the playlist as a whole; earlier observations stay outside the library."""
    pid = _playlist(store)
    store.playlists.publish_confirmed_tracks(pid, "Radio", [_track(0)], 2)
    before = store.playlists.get_playlist(pid)
    original = store.tracks.upsert_track
    def fail_second(video, *args, **kwargs):
        if video == "v2":
            raise RuntimeError("catalog failure")
        return original(video, *args, **kwargs)
    monkeypatch.setattr(store.tracks, "upsert_track", fail_second)
    with pytest.raises(RuntimeError, match="catalog failure"):
        store.playlists.publish_confirmed_tracks(pid, "Changed", [_track(i) for i in range(3)], 3,
                                                expected_prefix=["v0"] if incremental else None)
    assert store.playlists.get_playlist(pid) == before
    assert _videos(store, pid) == ["v0"]
    assert store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone()
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='v2'").fetchone()
    store.settings.set_setting('unrelated', 'committed')
    assert _library_videos(store) == {'v0'}
    monkeypatch.setattr(store.tracks, 'upsert_track', original)
    store.playlists.publish_confirmed_tracks(pid, 'Changed', [_track(i) for i in range(3)], 3,
                                            expected_prefix=['v0'] if incremental else None)
    assert _videos(store, pid) == ['v0', 'v1', 'v2']
    assert _library_videos(store) == {'v0', 'v1', 'v2'}


def test_other_connection_sees_only_complete_publication(file_backed_store, monkeypatch):
    store = file_backed_store
    path = store._db_path
    pid = _playlist(store)
    tracks = [_track(0)]
    store.playlists.publish_confirmed_tracks(pid, "Radio", tracks, 2)
    original = store.tracks.upsert_track
    with sqlite3.connect(path) as reader:
        def observed(video, *args, **kwargs):
            result = original(video, *args, **kwargs)
            assert reader.execute("SELECT title,track_count FROM playlists WHERE id=?",
                                  (pid,)).fetchone() == ("Radio", 1)
            assert reader.execute("SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position",
                                  (pid,)).fetchall() == [("v0",)]
            assert reader.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone()
            assert reader.execute('SELECT 1 FROM library_memberships m JOIN catalog_entries e ON e.id=m.entry_id '
                                  "WHERE e.video_id='v1'").fetchone() is None
            return result
        monkeypatch.setattr(store.tracks, "upsert_track", observed)
        store.playlists.publish_confirmed_tracks(pid, "Changed", [*tracks, _track(1)], 3,
                                                expected_prefix=["v0"])
        assert reader.execute("SELECT title,track_count FROM playlists WHERE id=?",
                              (pid,)).fetchone() == ("Changed", 2)
        assert reader.execute("SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position",
                              (pid,)).fetchall() == [("v0",), ("v1",)]


def test_stale_prefix_and_metadata_changes_reconcile_fully(store):
    pid = _playlist(store)
    store.playlists.publish_confirmed_tracks(pid, "Radio", [_track(0), _track(1)], 2)
    store.playlists.publish_confirmed_tracks(pid, "Renamed", [_track(1, album="New"), _track(2)], 3,
                                            expected_prefix=["v0"])
    assert _videos(store, pid) == ["v1", "v2"]
    assert store.playlists.get_playlist(pid).title == "Renamed"
    assert store.conn.execute("SELECT album FROM catalog_tracks WHERE video_id='v1'").fetchone()[0] == "New"
    store.playlists.publish_confirmed_tracks(pid, "Renamed", [_track(1, album="Updated"), _track(2)], 4)
    assert store.conn.execute("SELECT album FROM catalog_tracks WHERE video_id='v1'").fetchone()[0] == "Updated"


def test_membership_failure_cannot_publish_metadata_or_catalog(store):
    """An observed upload cannot become library content through a rejected playlist."""
    pid = _playlist(store)
    store.playlists.publish_confirmed_tracks(pid, "Radio", [_track(0)], 2)
    before = store.playlists.get_playlist(pid)
    with store.conn:
        store.conn.execute("CREATE TEMP TRIGGER reject_append BEFORE INSERT ON playlist_items "
                           "WHEN NEW.video_id='v1' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(Exception, match="injected failure"):
        store.playlists.publish_confirmed_tracks(pid, "Changed", [_track(0), _track(1)], 3,
                                                expected_prefix=["v0"])
    assert store.playlists.get_playlist(pid) == before
    assert _videos(store, pid) == ["v0"]
    assert store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone()
    store.settings.set_setting('unrelated', 'committed')
    assert _library_videos(store) == {'v0'}
    with store.conn:
        store.conn.execute('DROP TRIGGER reject_append')
    store.playlists.publish_confirmed_tracks(pid, 'Changed', [_track(0), _track(1)], 3,
                                            expected_prefix=['v0'])
    assert _videos(store, pid) == ['v0', 'v1']
    assert _library_videos(store) == {'v0', 'v1'}


@pytest.mark.parametrize("incremental", [False, True])
def test_late_membership_batch_failure_rolls_back_prior_batches(store, incremental):
    pid = _playlist(store)
    store.playlists.publish_confirmed_tracks(pid, "Radio", [_track(0)], 2)
    before = store.playlists.get_playlist(pid)
    retained = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY 1')))
                for table in ('youtube_items', 'recordings', 'catalog_entries')}
    tracks = [_track(i) for i in range(131)]
    with store.conn:
        store.conn.execute("CREATE TEMP TRIGGER reject_late_membership BEFORE INSERT ON playlist_items "
                           "WHEN NEW.video_id='v130' BEGIN SELECT RAISE(ABORT,'late membership failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="late membership failure"):
        store.playlists.publish_confirmed_tracks(
            pid, "Changed", tracks, 3, expected_prefix=["v0"] if incremental else None)
    assert not store.conn.in_transaction
    assert store.playlists.get_playlist(pid) == before
    assert _videos(store, pid) == ["v0"]
    for table, original_rows in retained.items():
        assert list(map(tuple, store.conn.execute(f'SELECT * FROM {table} WHERE '
                                                f'{"video_id" if table == "youtube_items" else "id"}=?',
                                                ('v0' if table == 'youtube_items' else 1,)))) == original_rows
        assert store.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == len(tracks)
    store.settings.set_setting('unrelated', 'committed')
    assert _library_videos(store) == {'v0'}
    with store.conn:
        store.conn.execute("DROP TRIGGER reject_late_membership")
    store.playlists.publish_confirmed_tracks(
        pid, "Changed", tracks, 3, expected_prefix=["v0"] if incremental else None)
    assert _videos(store, pid) == [track['video_id'] for track in tracks]
    assert _library_videos(store) == {track['video_id'] for track in tracks}
    assert store.conn.execute('SELECT COUNT(*) FROM catalog_entries').fetchone()[0] == len(tracks)
    playlist = store.playlists.get_playlist(pid)
    assert (playlist.title, playlist.track_count) == ("Changed", len(tracks))
    assert playlist.content_hash == content_hash(store.playlists.get_playlist_track_keys(pid))


@pytest.mark.parametrize("incremental", [False, True])
def test_publication_hash_uses_final_keys_after_upload_alias_collision(store, incremental):
    pid = _playlist(store)
    tracks = [_track(i, title="Same song", artist="Same artist") for i in range(2)]
    if incremental:
        store.playlists.publish_confirmed_tracks(pid, "Radio", tracks[:1], 2)
    store.playlists.publish_confirmed_tracks(
        pid, "Radio", tracks, 3, expected_prefix=["v0"] if incremental else None)
    keys = store.playlists.get_playlist_track_keys(pid)
    assert len(keys) == 2  # Different uploads must retain their distinct recording identities.
    assert store.playlists.get_playlist(pid).content_hash == content_hash(keys)
