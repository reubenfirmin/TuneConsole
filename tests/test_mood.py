from yt_playlist.rec import embed, recommend


def test_mood_demotes_but_does_not_banish_cluster_in_for_you(store):
    """A strong negative mood DE-RANKS the disfavored cluster in for_you — it drops out of the lane's
    top but stays present, because the transient tilt de-ranks rather than banishes (banishing is the
    job of dislike / sustained graduation, not one mood gesture)."""
    iid = store.upsert_identity("main", "cred", None, True)
    # two clusters by artist; both get vectors via artist baskets
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    pid = store.upsert_playlist(iid, "P", "P", 16, "h", 0.0)
    store.set_playlist_tracks(pid, A + B)
    embed.build_and_store(store, dim=4)
    now = 1000.0

    def neighbourhood(items):
        return [i.artist for i in items if i.lane == "neighbourhood"]

    # neutral run: A cluster appears near the top of the neighbourhood lane
    neutral = neighbourhood(recommend.for_you(store, now=now))
    neutral_a_top = sum(1 for a in neutral[:8] if a == "AB")
    assert neutral_a_top > 0, "A cluster should appear near the top before any mood"

    # a strong negative mood on A-cluster tracks
    store.record_mood(["a0|ab", "a1|ab", "a2|ab", "a3|ab"], -2, now)
    mood = neighbourhood(recommend.for_you(store, now=now))

    # DEMOTION: A is pushed out of the lane's top relative to the neutral baseline
    mood_a_top = sum(1 for a in mood[:8] if a == "AB")
    assert mood_a_top < neutral_a_top, "negative mood on A should demote it out of the lane's top"
    # NOT BANISHED: A is still present in the lane (de-rank, not removal)
    assert "AB" in mood, "A is de-ranked, not banished — it should still be present in the lane"
    # B fills the freed-up top
    assert "BB" in mood, "B should fill the top once A is demoted"
