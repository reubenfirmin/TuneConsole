from yt_playlist import embed, recommend


def test_catalog_ranks_under_played_first_weighted_not_filtered(store):
    """Catalog ('explore_for_you') surfaces your under-played catalog FIRST, weighted (not filtered)
    by taste: unplayed tracks rank above played ones, and played tracks are de-ranked but NOT
    excluded (no artist filter)."""
    iid = store.upsert_identity("main", "cred", None, True)
    # same artist for both sets, so PLAYS (not taste/artist) is what differs
    played = [store.upsert_track(f"p{i}", f"P{i}", "Art", None, None) for i in range(6)]
    unplayed = [store.upsert_track(f"u{i}", f"U{i}", "Art", None, None) for i in range(6)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 12, "h", 0.0), played + unplayed)
    for k in [f"p{i}|art" for i in range(6)]:
        store.add_history_snapshot(iid, 1.0, [k])      # the P set has plays; the U set has none
    embed.build_and_store(store, dim=4)

    cat = recommend.explore_for_you(store, now=2.0, limit=12)
    assert cat and cat[0].title.startswith("U"), "the top Catalog pick should be an unplayed track"
    # unplayed dominate the top half (lack-of-plays is the primary signal)
    assert sum(1 for i in cat[:6] if i.title.startswith("U")) >= 5
    # NOT filtered: a played track is de-ranked but can still appear lower down
    assert any(i.title.startswith("P") for i in cat), "played tracks are de-ranked, not excluded"
    assert all(i.lane == "explore" for i in cat)


def test_explore_empty_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    assert recommend.explore_for_you(store, now=1.0) == []
