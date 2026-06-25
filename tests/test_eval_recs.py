from yt_playlist.rec import embed, eval_recs


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
    assert int(store.get_setting("rec_dim")) == res["winner"]["dim"]
    assert store.get_setting("rec_embed_method") == res["winner"]["method"]
    assert "recall" in res["previous"]
