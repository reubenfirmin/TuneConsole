from yt_playlist.rec import eval_recs


def test_autotune_ab_method_and_dim(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)

    res = eval_recs.autotune(store, dims=(4, 6), methods=("svd", "item2vec"), k=5)
    assert res["best_method"] in ("svd", "item2vec")
    assert res["best_dim"] in (4, 6)
    assert len(res["scores"]) == 4                       # 2 methods × 2 dims
    assert store.get_setting("rec_embed_method") == res["best_method"]
    assert store.get_setting("rec_dim") == str(res["best_dim"])
    assert store.rec_vectors_count() > 0                 # left on a built model
