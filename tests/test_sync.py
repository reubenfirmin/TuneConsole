from yt_playlist.sync import sync_identity, content_hash
from tests.conftest import FakeClient, _track


def test_content_hash_is_order_independent():
    assert content_hash(["a", "b"]) == content_hash(["b", "a"])
    assert content_hash(["a", "b"]) != content_hash(["a", "c"])

def test_sync_identity_populates_store(store):
    iid = store.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Mix", "count": 2}],
        tracks={"PL1": [_track("v1", "Song A", "Artist"), _track("v2", "Song B", "Artist")]},
        history=[_track("v1", "Song A", "Artist")])
    sync_identity(store, iid, client, now=1000.0)

    pls = store.get_playlists()
    assert len(pls) == 1
    assert store.get_playlist_track_keys(pls[0].id) == {"song a|artist", "song b|artist"}
    assert store.get_recent_history_keys(0.0) == {"song a|artist"}
    assert pls[0].content_hash == content_hash(["song a|artist", "song b|artist"])

def test_sync_identity_no_truncation_beyond_defaults(store):
    """Regression: sync_identity must pass limit=None so >25 playlists and >100 tracks are
    not silently truncated by ytmusicapi's defaults."""
    iid = store.upsert_identity("main", "cred", None, True)

    # 30 playlists — exceeds the get_library_playlists default of 25
    playlists = [{"playlistId": f"PL{i}", "title": f"Playlist {i}", "count": 150}
                 for i in range(30)]

    # 150 tracks in each playlist — exceeds the get_playlist default of 100
    tracks_per_pl = {
        f"PL{i}": [_track(f"v{i}_{j}", f"Song {j}", "Artist") for j in range(150)]
        for i in range(30)
    }

    client = FakeClient(playlists=playlists, tracks=tracks_per_pl, history=[])
    sync_identity(store, iid, client, now=2000.0)

    pls = store.get_playlists()
    assert len(pls) == 30, f"Expected 30 playlists but got {len(pls)} (truncation bug?)"
    for pl in pls:
        keys = store.get_playlist_track_keys(pl.id)
        assert len(keys) == 150, (
            f"Playlist {pl.remote_id} has {len(keys)} track keys, expected 150 (truncation bug?)"
        )


def test_sync_prunes_playlists_gone_from_remote(store):
    import yt_playlist.sync as sync
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    # stale row in the store that the remote library no longer lists
    ghost = store.upsert_playlist(iid, "GHOST", "Deleted Already", 1, "h", 1.0)
    store.set_playlist_tracks(ghost, [store.upsert_track("vx", "Old", "X", None, 1)])
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Live", "count": 1}],
        tracks={"PL1": [_track("v1", "A", "X")]})
    sync.sync_identity(store, iid, client, now=1.0)
    ytm_ids = {p.ytm_playlist_id for p in store.get_playlists()}
    assert ytm_ids == {"PL1"}                 # GHOST pruned, PL1 present
    assert store.get_playlist(ghost) is None
