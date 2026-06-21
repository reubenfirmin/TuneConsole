from yt_playlist import embed, recommend


def test_playlist_taste_weights_by_play_and_scores(store):
    iid = store.upsert_identity("main", "cred", None, True)
    techno = [store.upsert_track(f"t{i}", f"T{i}", "TechnoBand", None, None) for i in range(8)]
    folk = [store.upsert_track(f"f{i}", f"F{i}", "FolkBand", None, None) for i in range(8)]
    pt = store.upsert_playlist(iid, "PT", "Techno Nights", 8, "h", 0.0)
    pf = store.upsert_playlist(iid, "PF", "Dad Vacation", 8, "h2", 0.0)
    store.set_playlist_tracks(pt, techno)
    store.set_playlist_tracks(pf, folk)
    for _ in range(5):                                    # you live in the techno playlist
        store.add_history_snapshot(iid, 1.0, ["t0|technoband"])
    store.add_history_snapshot(iid, 1.0, ["f0|folkband"])  # the Dad playlist barely played
    embed.build_and_store(store, dim=4)

    pt_model = recommend.playlist_taste(store)
    assert pt_model                                       # non-empty
    keys, V, idx = embed.load_vectors(store)
    s_techno, because = pt_model.score(V[idx["t1|technoband"]])
    s_folk, _ = pt_model.score(V[idx["f1|folkband"]])

    assert s_techno > s_folk                              # techno candidate beats the low-play folk one
    assert because[0][0] == "Techno Nights"               # explained by the playlist you actually play


def test_playlist_taste_empty_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    pt = recommend.playlist_taste(store)
    assert not pt and pt.score([1.0]) == (0.0, [])
