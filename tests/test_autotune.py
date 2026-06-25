from yt_playlist.rec import eval_recs


def test_autotune_ab_leaves_built_winner(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Corpus large enough that the smaller grid dims (48/64/96) can build.
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(60)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(60)]
    for j in range(8):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PA{j}", "PA", 8, f"ha{j}", 0.0), A[j*5:j*5+8])
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PB{j}", "PB", 8, f"hb{j}", 0.0), B[j*5:j*5+8])

    res = eval_recs.autotune(store)
    assert res["winner"]["method"] in ("svd", "item2vec")
    assert res["winner"]["dim"] in (48, 64, 96, 128)
    assert store.get_setting("rec_embed_method") == res["winner"]["method"]
    assert store.get_setting("rec_dim") == str(res["winner"]["dim"])
    assert store.rec_vectors_count() > 0                 # left on a built model
