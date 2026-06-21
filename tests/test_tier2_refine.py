from yt_playlist import embed, recommend


def test_for_you_applies_tier2_sim_ranking(store, monkeypatch):
    """With vectors present, for_you re-orders candidates by taste-centroid similarity."""
    iid = store.upsert_identity("main", "cred", None, True)
    hx = store.upsert_track("hx", "HX", "X", None, None)
    store.upsert_track("dx", "DeepX", "X", None, None)        # deep cut for artist X
    hy = store.upsert_track("hy", "HY", "Y", None, None)
    store.upsert_track("dy", "DeepY", "Y", None, None)        # deep cut for artist Y
    filler = [store.upsert_track(f"f{i}", f"F{i}", "Filler", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 2, "h", 0.0), [hx, hy])
    store.set_playlist_tracks(store.upsert_playlist(iid, "PF", "PF", 8, "h2", 0.0), filler)
    store.add_history_snapshot(iid, 1.0, ["hx|x", "hy|y"])
    embed.build_and_store(store, dim=4)                       # >=9 tracks -> vectors -> Tier-2 active
    assert store.rec_vectors_count() > 0

    monkeypatch.setattr(embed, "sims_for", lambda *a, **k: {"deepx|x": 0.1, "deepy|y": 0.9})
    keys = [i.key for i in recommend.for_you(store, now=2.0, limit=10)]

    assert "deepx|x" in keys and "deepy|y" in keys
    assert keys.index("deepy|y") < keys.index("deepx|x")     # higher similarity ranks first


def test_for_you_unaffected_without_vectors(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Fav", None, None)
    store.upsert_track("v2", "Bench", "Fav", None, None)
    store.add_history_snapshot(iid, 1.0, ["hit|fav"])
    assert recommend.for_you(store, now=2.0)                 # no vectors -> still works (no Tier-2)
