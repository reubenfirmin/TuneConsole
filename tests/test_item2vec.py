import numpy as np

from yt_playlist.rec import embed


def test_item2vec_builds_unit_vectors(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    store.set_setting("rec_embed_method", "item2vec")

    assert embed.build_and_store(store, dim=8) == 16
    keys, V, idx = embed.load_vectors(store)
    assert V.shape == (16, 8)
    assert np.allclose(np.linalg.norm(V, axis=1), 1.0, atol=1e-3)   # L2-normalized
    assert embed.neighbors(store, "a0|ab", topn=3)                  # produces neighbours
    # quality is measured by recall@k in autotune, not asserted here (noisy on tiny data)


def test_method_setting_switches_builder(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(14)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 14, "h", 0.0), A)
    store.set_setting("rec_embed_method", "svd")
    assert embed.build_and_store(store, dim=8) == 14
    store.set_setting("rec_embed_method", "item2vec")
    assert embed.build_and_store(store, dim=8) == 14      # both methods build cleanly
