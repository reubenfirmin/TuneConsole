from yt_playlist.rec import embed, recommend
from yt_playlist.util.matching import identity_key


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


def test_neighbors_for_unmodeled_uses_artist_proxy(store):
    # A generated (quarantined) track by artist 'AB' gets no vector of its own, but 'songs like this'
    # should still work, proxied through the AB tracks that ARE in the model.
    iid, A, B = _two_clusters(store)
    g = store.upsert_track("gnew", "GNEW", "AB", None, None)
    gpl = store.upsert_playlist(iid, "PG", "Gen", 1, "h2", 0.0)
    store.set_playlist_tracks(gpl, [g])
    store.set_playlist_group("PG", "Generated")
    embed.build_and_store(store, dim=4)

    seed = identity_key("GNEW", "AB")
    assert embed.neighbors(store, seed) == []                   # quarantined: no vector of its own
    nbrs = embed.neighbors_for_unmodeled(store, seed, topn=4)
    assert nbrs and all(k.endswith("|ab") for k, _ in nbrs)     # proxied to the artist's A-cluster


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


# --- content-space fingerprint -------------------------------------------------------------------
# Taste-mode centroids are persisted vectors in this space; the fingerprint is how they detect that
# the space they were built in no longer exists (see rec/taste_modes.reconcile).

def test_fingerprint_is_stable_for_the_same_space():
    m = {"cat": {"rock": 0, "pop": 1}, "cont": [["bpm", 120.0, 10.0]]}
    assert embed.content_model_fingerprint(m) == embed.content_model_fingerprint(dict(m))


def test_fingerprint_changes_when_a_token_is_added():
    a = {"cat": {"rock": 0, "pop": 1}, "cont": []}
    b = {"cat": {"rock": 0, "pop": 1, "jazz": 2}, "cont": []}
    assert embed.content_model_fingerprint(a) != embed.content_model_fingerprint(b)


def test_fingerprint_changes_when_columns_are_reordered():
    """Same dimension, different basis: the case a shape check cannot catch."""
    a = {"cat": {"rock": 0, "pop": 1}, "cont": []}
    b = {"cat": {"rock": 1, "pop": 0}, "cont": []}
    assert embed.content_model_fingerprint(a) != embed.content_model_fingerprint(b)


def test_fingerprint_changes_when_a_continuous_feature_drops_out():
    a = {"cat": {}, "cont": [["bpm", 120.0, 10.0], ["energy", 0.5, 0.1]]}
    b = {"cat": {}, "cont": [["bpm", 120.0, 10.0]]}
    assert embed.content_model_fingerprint(a) != embed.content_model_fingerprint(b)


def test_fingerprint_ignores_zscore_drift():
    """mu/sd move on every rebuild; treating that as a new space would retire all modes each pass."""
    a = {"cat": {"rock": 0}, "cont": [["bpm", 120.0, 10.0]]}
    b = {"cat": {"rock": 0}, "cont": [["bpm", 118.4, 11.2]]}
    assert embed.content_model_fingerprint(a) == embed.content_model_fingerprint(b)
