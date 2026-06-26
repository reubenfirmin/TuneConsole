from yt_playlist.rec import embed, eval_recs
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def test_recall_recovers_held_out_cluster_track(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # one tight 8-track cluster (one playlist) + a distractor cluster, so the held-out
    # track should rank near the rest of its own playlist.
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    embed.build_and_store(store, dim=4)

    res = eval_recs.recall_at_k(store, k=5, min_size=5)
    assert res["trials"] == 2
    assert res["recall_at_k"] == 1.0          # both held-out tracks recovered in top-5


def test_recall_none_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    assert eval_recs.recall_at_k(store)["recall_at_k"] is None


def _two_cluster_playlists(store):
    """Two tight 8-track clusters, one per playlist, so the embedding separates A from B. Returns iid."""
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    embed.build_and_store(store, dim=4)
    return iid


def test_temporal_recall_recovers_recent_plays_from_history(store):
    # The model's real job: predict what you played next. Context = cluster-A tracks played before the
    # holdout window; held-out = the rest of cluster A played inside it. A tight cluster should rank its
    # own held-out members near the context centroid, so recall is high.
    iid = _two_cluster_playlists(store)
    day, t_recent = 86400, 1_000_000.0
    store.add_history_snapshot(iid, t_recent - 10 * day, [identity_key(f"A{i}", "AB") for i in range(4)])
    store.add_history_snapshot(iid, t_recent, [identity_key(f"A{i}", "AB") for i in range(4, 8)])

    res = eval_recs.temporal_recall(store, holdout_days=5, k=8)
    assert res["trials"] == 4                 # the four held-out recent plays, none seen before the cutoff
    assert res["recall"] == 1.0               # all recovered in the top-8 by the context centroid


def test_temporal_recall_none_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    assert eval_recs.temporal_recall(store)["recall"] is None


def test_temporal_recall_none_without_history(store):
    _two_cluster_playlists(store)             # vectors exist, but no history snapshots were taken
    assert eval_recs.temporal_recall(store)["recall"] is None


def test_projection_recall_breakdown_by_family_and_coverage(store):
    # The scalar projection_recall hides where grounding fails. The breakdown must partition trials by
    # genre family, era, and coverage band so the failure modes are locatable.
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Techno")
        store.set_track_year(t, "2015")       # cluster A has a year (coverage band: genre+year)
    for t in B:
        store.set_track_genre(t, "Folk")      # cluster B has no year (coverage band: genre only)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    embed.build_and_store(store, dim=8)

    r = eval_recs.projection_recall(store, k=10)
    bd = r["breakdown"]
    assert genre_map.family("Techno") in bd["by_family"]
    assert genre_map.family("Folk") in bd["by_family"]
    assert sum(b["trials"] for b in bd["by_family"].values()) == r["trials"]   # a partition of trials
    assert set(bd["by_coverage"]) == {"genre+year", "genre"}   # §2 widened the band taxonomy (+audio)
    assert bd["by_coverage"]["genre+year"]["trials"] == 10
    assert bd["by_coverage"]["genre"]["trials"] == 10


def test_autotune_returns_grid_and_picks_best(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Two tight clusters across several playlists so recall is well-defined and svd wins.
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(40)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(40)]
    for j in range(6):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PA{j}", "PA", 8, f"ha{j}", 0.0), A[j*5:j*5+8])
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PB{j}", "PB", 8, f"hb{j}", 0.0), B[j*5:j*5+8])

    res = eval_recs.autotune(store)

    # grid present, every entry shaped, dims limited to the new grid
    assert res["grid"], "grid must be non-empty"
    dims = {g["dim"] for g in res["grid"] if g["method"] == "svd"}
    assert dims == {48, 64, 96, 128}
    assert any(g["method"] == "item2vec" for g in res["grid"])   # one sanity probe
    # winner is the max-recall grid entry, and was persisted
    best = max(res["grid"], key=lambda g: g["recall"])
    assert res["winner"]["dim"] == best["dim"] and res["winner"]["method"] == best["method"]


def test_autotune_falls_back_to_recall_at_k_without_history(store):
    # No history snapshots -> no usable temporal split -> autotune scores configs by recall@k (#38 §5).
    _two_cluster_playlists(store)
    res = eval_recs.autotune(store, svd_dims=(4,), item2vec_probe_dim=4, k=5)
    assert res["grid"]
    assert all(g.get("metric") == "recall_at_k" for g in res["grid"]), "no history -> recall@k fallback"
    assert res["previous"].get("metric") == "recall_at_k"


def test_autotune_scores_by_temporal_recall_when_history_spans_the_window(store):
    # #38 §5: the method/DIM sweep must be judged on the model's real job (predict next plays), not the
    # in-sample recall@k. With a history span wider than the holdout window, autotune uses temporal_recall.
    iid = _two_cluster_playlists(store)
    day, t = 86400, 5_000_000.0
    store.add_history_snapshot(iid, t - 40 * day, [identity_key(f"A{i}", "AB") for i in range(4)])
    store.add_history_snapshot(iid, t, [identity_key(f"A{i}", "AB") for i in range(4, 8)])
    res = eval_recs.autotune(store, svd_dims=(4,), item2vec_probe_dim=4, k=8)
    assert res["grid"]
    assert all(g.get("metric") == "temporal_recall" for g in res["grid"]), \
        "with a usable temporal split, autotune scores configs by forward-prediction recall"


def test_cooc_weighting_ab_compares_both_builds(store):
    # #38 §4c: the A/B harness builds the embedding binary AND playcount-weighted, scores each, and
    # names a winner. The verdict is real-data-only; here we just assert the comparison shape.
    _two_cluster_playlists(store)
    res = eval_recs.cooc_weighting_ab(store, k=5)
    assert set(res) >= {"binary", "weighted", "winner"}
    assert res["winner"] in ("binary", "weighted")
    assert "score" in res["binary"] and "metric" in res["weighted"]
    assert store.get_setting("rec_cooc_weighting") in (None, "0")   # prior setting restored
