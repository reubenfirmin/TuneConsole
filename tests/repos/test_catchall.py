"""catchall_playlist_ids excludes playlists too large to be a coherent listening context (size-based).
(A #38 genre-coherence variant was tried and reverted: temporal_recall showed no gain.)"""
from yt_playlist.core.store import Store


def _trk(store, vid, title, artist):
    return store.upsert_track(vid, title, artist, None, None)


def test_catchall_is_size_based(store):
    iid = store.upsert_identity("m", "c", None, True)
    big = store.upsert_playlist(iid, "ytm_big", "Big", 4, "h", 0.0)
    store.set_playlist_tracks(big, [_trk(store, f"b{i}", f"B{i}", "A") for i in range(4)])
    small = store.upsert_playlist(iid, "ytm_small", "Small", 2, "h", 0.0)
    store.set_playlist_tracks(small, [_trk(store, "s0", "S0", "B"), _trk(store, "s1", "S1", "C")])

    catchall = store.catchall_playlist_ids(size_floor=3)
    assert big in catchall and small not in catchall
