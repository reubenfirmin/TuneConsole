from yt_playlist import recommend


def test_tight_playlist_has_zero_diversity(store):
    iid = store.upsert_identity("main", "cred", None, True)
    ts = [store.upsert_track(f"t{i}", f"T{i}", "A", None, None) for i in range(3)]
    for t in ts:
        store.set_track_genre(t, "Techno")        # all one genre
    pid = store.upsert_playlist(iid, "P", "Tight", 3, "h", 0.0)
    store.set_playlist_tracks(pid, ts)
    d = recommend.playlist_genre_diversity(store, pid)
    assert d["median"] == 0.0 and d["max"] == 0.0 and d["n_tagged"] == 3


def test_eclectic_playlist_has_high_diversity(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("a", "A", "X", None, None); store.set_track_genre(a, "Techno")
    b = store.upsert_track("b", "B", "Y", None, None); store.set_track_genre(b, "Hard Rock")
    pid = store.upsert_playlist(iid, "P", "Mix", 2, "h", 0.0)
    store.set_playlist_tracks(pid, [a, b])
    assert recommend.playlist_genre_diversity(store, pid)["median"] == 1.0


def test_diversity_none_when_under_two_tagged(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("a", "A", "X", None, None)   # untagged
    b = store.upsert_track("b", "B", "Y", None, None); store.set_track_genre(b, "Techno")
    pid = store.upsert_playlist(iid, "P", "P", 2, "h", 0.0)
    store.set_playlist_tracks(pid, [a, b])
    assert recommend.playlist_genre_diversity(store, pid) is None
