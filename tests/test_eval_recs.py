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
