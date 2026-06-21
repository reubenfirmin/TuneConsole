from yt_playlist import embed, recommend


def test_explore_excludes_familiar_artists(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # a cluster: "Fav" you play a lot, "NewBand" you never play, both in one playlist
    fav = [store.upsert_track(f"f{i}", f"F{i}", "Fav", None, None) for i in range(6)]
    new = [store.upsert_track(f"n{i}", f"N{i}", "NewBand", None, None) for i in range(6)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 12, "h", 0.0), fav + new)
    for k in ["f0|fav", "f1|fav", "f2|fav"]:
        store.add_history_snapshot(iid, 1.0, [k])     # only Fav is played
    embed.build_and_store(store, dim=4)

    ex = recommend.explore_for_you(store, now=2.0, limit=10)
    artists = {i.artist for i in ex}
    assert "Fav" not in artists          # your familiar artist is excluded
    assert all(i.lane == "explore" for i in ex)


def test_explore_empty_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    assert recommend.explore_for_you(store, now=1.0) == []
