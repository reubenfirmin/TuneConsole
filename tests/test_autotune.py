from yt_playlist import eval_recs


def test_autotune_picks_dim_and_persists(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)

    res = eval_recs.autotune(store, dims=(4, 6), k=5)
    assert res["best_dim"] in (4, 6)
    assert set(res["scores"]) == {4, 6}
    assert store.get_setting("rec_dim") == str(res["best_dim"])   # persisted
    assert store.rec_vectors_count() > 0                          # left on a built model
