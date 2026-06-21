from yt_playlist import embed, recommend


def _two_clusters(store):
    """Two disjoint 6-track clusters (A in playlist PA, B in PB). identity_key = 'a0|ab' etc."""
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(6)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(6)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 6, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 6, "h2", 0.0), B)
    return iid, A, B


def test_embedding_neighbors_stay_in_cluster(store):
    _two_clusters(store)
    n = embed.build_and_store(store, dim=4)
    assert n == 12
    nbrs = embed.neighbors(store, "a0|ab", topn=4)
    assert nbrs, "expected neighbours once vectors are built"
    assert all(k.endswith("|ab") for k, _ in nbrs)   # A-cluster only, never B


def test_neighbors_empty_before_build(store):
    _two_clusters(store)
    assert embed.neighbors(store, "a0|ab") == []      # no vectors yet -> no neighbours


def test_for_you_uses_taste_neighbourhood_when_built(store):
    iid, A, _ = _two_clusters(store)
    store.add_history_snapshot(iid, 1.0, ["a0|ab", "a1|ab"])   # you play the A cluster
    embed.build_and_store(store, dim=4)
    items = recommend.for_you(store, now=1000.0)
    assert any(i.reason == "In your taste neighbourhood" for i in items)


def test_complete_playlist_uses_embedding_centroid(store):
    iid, A, B = _two_clusters(store)
    target = store.upsert_playlist(iid, "PT", "Target", 2, "h3", 0.0)
    store.set_playlist_tracks(target, [A[0], A[1]])             # seed with two A tracks
    embed.build_and_store(store, dim=4)
    items = recommend.complete_playlist(store, target, limit=4)
    assert items
    assert all(i.artist == "AB" for i in items)                # centroid pulls A-cluster, not B
