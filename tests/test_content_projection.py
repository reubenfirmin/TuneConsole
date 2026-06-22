import numpy as np

from yt_playlist.rec import discover, embed, eval_recs


def test_content_projection_lands_in_right_cluster(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Techno")
    for t in B:
        store.set_track_genre(t, "Folk")
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    embed.build_and_store(store, dim=8)

    proj = discover.ContentProjection.fit(store)
    assert proj is not None
    keys, V, idx = embed.load_vectors(store)
    p = proj.predict("Techno")
    p = p / (np.linalg.norm(p) + 1e-9)
    def avg(suffix):
        return np.mean([V[idx[k]] / np.linalg.norm(V[idx[k]]) @ p for k in idx if k.endswith(suffix)])
    assert avg("|ab") > avg("|bb")              # a Techno candidate predicts into the techno cluster

    r = eval_recs.projection_recall(store, k=10)
    assert r["recall"] is not None and r["trials"] == 20


def test_projection_none_without_enough_tags(store):
    store.upsert_identity("main", "cred", None, True)
    assert discover.ContentProjection.fit(store) is None
