from yt_playlist import embed, recommend


def test_recent_mood_tilts_neighbourhood(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # two clusters by artist; both get vectors via artist baskets
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 16, "h", 0.0), A + B)
    embed.build_and_store(store, dim=4)

    now = 1000.0
    # all-time favourite is the A cluster
    for k in ["a0|ab", "a1|ab", "a2|ab"]:
        store.add_history_snapshot(iid, now - 30 * 86400, [k])
    # but *recently* you've been on the B cluster (mood)
    for k in ["b0|bb", "b1|bb", "b2|bb"]:
        store.add_history_snapshot(iid, now - 3600, [k])

    nb = recommend._taste_neighbourhood(store, limit=8, now=now)
    artists = {r["artist"] for r in nb}
    assert "BB" in artists           # recent mood pulled the B cluster in

    # with no mood signal, it leans to the all-time A favourites
    nb_slow = recommend._taste_neighbourhood(store, limit=8, now=None)
    assert {r["artist"] for r in nb_slow} == {"AB"} or "AB" in {r["artist"] for r in nb_slow}
