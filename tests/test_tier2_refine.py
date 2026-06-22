from yt_playlist.rec import embed, recommend


def test_for_you_tier2_prefers_high_play_context(store):
    """Tier-2 re-ranks the feed by play-weighted per-playlist taste: a deep cut from a heavily-
    played context outranks one from a barely-played context."""
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AX", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BX", None, None) for i in range(10)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    for _ in range(8):                                   # context A is heavily played
        store.add_history_snapshot(iid, 1.0, ["a0|ax"])
    store.add_history_snapshot(iid, 1.0, ["b0|bx"])      # context B barely
    embed.build_and_store(store, dim=8)

    keys = [i.key for i in recommend.for_you(store, now=2.0, limit=12)]
    a_cut = next((k for k in keys if k.endswith("|ax")), None)
    b_cut = next((k for k in keys if k.endswith("|bx")), None)
    assert a_cut and b_cut
    assert keys.index(a_cut) < keys.index(b_cut)


def test_for_you_unaffected_without_vectors(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Fav", None, None)
    store.upsert_track("v2", "Bench", "Fav", None, None)
    store.add_history_snapshot(iid, 1.0, ["hit|fav"])
    assert recommend.for_you(store, now=2.0)             # no vectors -> still works (no Tier-2)
