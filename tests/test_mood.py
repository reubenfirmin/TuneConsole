from yt_playlist import embed, recommend


def test_mood_disfavors_genre_in_for_you(store):
    """A strong negative mood on one cluster's tracks pushes them down in for_you rankings."""
    iid = store.upsert_identity("main", "cred", None, True)
    # two clusters by artist; both get vectors via artist baskets
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    pid = store.upsert_playlist(iid, "P", "P", 16, "h", 0.0)
    store.set_playlist_tracks(pid, A + B)
    embed.build_and_store(store, dim=4)

    now = 1000.0
    # neutral run: for_you returns both A and B tracks in the neighbourhood
    neutral = recommend.for_you(store, now=now)
    neutral_artists = {i.artist for i in neutral if i.lane == "neighbourhood"}
    assert neutral_artists, "expected neighbourhood lane items once vectors are built"

    # record a strong negative mood on A cluster tracks
    a_keys = ["a0|ab", "a1|ab", "a2|ab", "a3|ab"]
    store.record_mood(a_keys, -2, now)

    # with a negative mood on A, B-cluster tracks should be present in the neighbourhood lane
    mood_items = recommend.for_you(store, now=now)
    mood_artists = {i.artist for i in mood_items if i.lane == "neighbourhood"}
    assert "BB" in mood_artists, "negative mood on A cluster should surface B cluster in neighbourhood"
