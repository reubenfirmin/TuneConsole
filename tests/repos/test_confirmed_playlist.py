import sqlite3

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.store import Store
from yt_playlist.library.playlist_metadata import content_hash


def _track(i, **extra):
    return {"video_id": f"v{i}", "title": f"Song {i}", "artist": f"Artist {i}", "album": "Album", **extra}


def _playlist(store):
    identity = store.identities.upsert_identity("main", "ref", None, True)
    pid = store.playlists.upsert_playlist(identity, "RADIO", "Radio", 0, "", 1)
    store.playlists.mark_generated_playlist("RADIO")
    return pid


def _videos(store, pid):
    return [r[0] for r in store.conn.execute(
        "SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (pid,))]


@pytest.mark.parametrize("size", [8, 1024])
def test_append_writes_only_new_catalog_and_membership(store, monkeypatch, size):
    pid = _playlist(store)
    tracks = [_track(i) for i in range(size)]
    store.playlists.publish_confirmed_tracks(pid, "Radio", tracks, 2)
    with store.conn:
        store.conn.execute("CREATE TEMP TABLE publication_writes(kind TEXT)")
        for operation in ["INSERT", "DELETE", "UPDATE"]:
            store.conn.execute(f"CREATE TEMP TRIGGER count_playlist_{operation} AFTER {operation} ON playlist_items "
                               f"BEGIN INSERT INTO publication_writes VALUES ('{operation}'); END")
    original, calls = catalog.upsert, []
    def observed(conn, video, *args, **kwargs):
        calls.append(video)
        return original(conn, video, *args, **kwargs)
    monkeypatch.setattr(catalog, "upsert", observed)
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
    pid = _playlist(store)
    store.playlists.publish_confirmed_tracks(pid, "Radio", [_track(0)], 2)
    before = store.playlists.get_playlist(pid)
    original = catalog.upsert
    def fail_second(conn, video, *args, **kwargs):
        if video == "v2":
            raise RuntimeError("catalog failure")
        return original(conn, video, *args, **kwargs)
    monkeypatch.setattr(catalog, "upsert", fail_second)
    with pytest.raises(RuntimeError, match="catalog failure"):
        store.playlists.publish_confirmed_tracks(pid, "Changed", [_track(i) for i in range(3)], 3,
                                                expected_prefix=["v0"] if incremental else None)
    assert store.playlists.get_playlist(pid) == before
    assert _videos(store, pid) == ["v0"]
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone()


def test_other_connection_sees_only_complete_publication(tmp_path, monkeypatch):
    path = tmp_path / 'publication.db'
    store = Store(path)
    try:
        store.init_schema()
        pid = _playlist(store)
        tracks = [_track(0)]
        store.playlists.publish_confirmed_tracks(pid, "Radio", tracks, 2)
        original = catalog.upsert
        with sqlite3.connect(path) as reader:
            def observed(conn, video, *args, **kwargs):
                result = original(conn, video, *args, **kwargs)
                assert reader.execute("SELECT title,track_count FROM playlists WHERE id=?",
                                      (pid,)).fetchone() == ("Radio", 1)
                assert reader.execute("SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position",
                                      (pid,)).fetchall() == [("v0",)]
                assert reader.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone() is None
                return result
            monkeypatch.setattr(catalog, "upsert", observed)
            store.playlists.publish_confirmed_tracks(pid, "Changed", [*tracks, _track(1)], 3,
                                                    expected_prefix=["v0"])
            assert reader.execute("SELECT title,track_count FROM playlists WHERE id=?",
                                  (pid,)).fetchone() == ("Changed", 2)
            assert reader.execute("SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position",
                                  (pid,)).fetchall() == [("v0",), ("v1",)]
    finally:
        store.conn.close()


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
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='v1'").fetchone()


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
