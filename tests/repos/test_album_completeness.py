"""Album completeness has the same rules for bulk planning and scoped reads."""
import pytest

from yt_playlist.library.album_catalog import remember_album


def _cache_album(store, browse_id):
    video_id = f"video-{browse_id}"
    entry = store.tracks.upsert_track(video_id, browse_id, "Artist", "Album", 10)
    remember_album(store, browse_id, {
        "title": "Album", "artists": [{"name": "Artist"}], "trackCount": 2,
        "tracks": [{"videoId": video_id, "title": browse_id}] * 2})
    return entry


@pytest.mark.parametrize("damage", [None, "fetch-state", "empty", "count", "membership", "unresolved"])
def test_scoped_completeness_preserves_full_occurrence_and_membership_rules(store, damage):
    entry = _cache_album(store, "target")
    _cache_album(store, "unrelated")
    album_id = store.collection.album_id("target")
    with store.conn:
        if damage == "fetch-state":
            store.conn.execute("UPDATE album_fetch_state SET complete=0 WHERE album_id=?", (album_id,))
        elif damage == "empty":
            store.conn.execute("DELETE FROM album_tracks WHERE album_id=?", (album_id,))
            store.conn.execute("UPDATE album_fetch_state SET expected_track_count=0 WHERE album_id=?", (album_id,))
        elif damage == "count":
            store.conn.execute("DELETE FROM album_tracks WHERE album_id=? AND position=1", (album_id,))
        elif damage == "membership":
            store.conn.execute("DELETE FROM library_memberships WHERE entry_id=?", (entry,))
        elif damage == "unresolved":
            store.conn.execute("UPDATE album_tracks SET entry_id=NULL,legacy_track_key='unknown|artist' "
                               "WHERE album_id=? AND position=1", (album_id,))

    complete = damage is None
    assert store.collection.is_album_tracklist_complete("target") is complete
    assert store.collection.is_album_tracklist_complete("unrelated")
    assert not store.collection.is_album_tracklist_complete("absent")
    assert store.collection.complete_album_tracklist_ids() == (
        {"target", "unrelated"} if complete else {"unrelated"})
    share = store.collection.cached_album_share("target")
    assert (share is not None) is complete
    if share:
        assert share["video_ids"] == ["video-target", "video-target"]


def _sqlite_steps(store, read):
    """Count SQLite work on this disposable fixture, without a wall-clock threshold."""
    steps = 0

    def tick():
        nonlocal steps
        steps += 1
        return 0

    store.conn.set_progress_handler(tick, 1)
    try:
        result = read()
        return result, steps
    finally:
        store.conn.set_progress_handler(None, 0)


@pytest.mark.parametrize("read", ["complete", "share"])
def test_single_album_reads_do_not_grow_with_unrelated_cached_releases(store, read):
    entry = _cache_album(store, "target")
    lookup = (lambda: store.collection.is_album_tracklist_complete("target")) if read == "complete" else (
        lambda: store.collection.cached_album_share("target"))
    expected, before = _sqlite_steps(store, lookup)
    for index in range(100):
        store.collection.cache_album_tracklist(
            f"unrelated-{index}", "Other", "Artist", None, [], entries=[{"entry_id": entry}])
    actual, after = _sqlite_steps(store, lookup)
    assert actual == expected
    assert after <= 2 * before
