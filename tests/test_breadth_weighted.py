from yt_playlist import genre_map, recommend


def test_breadth_palette_are_play_weighted(store):
    iid = store.upsert_identity("main", "cred", None, True)
    techno = [store.upsert_track(f"t{i}", f"T{i}", "A", None, None) for i in range(6)]
    folk = [store.upsert_track(f"f{i}", f"F{i}", "B", None, None) for i in range(6)]
    for t in techno:
        store.set_track_genre(t, "Techno")
    for f in folk:
        store.set_track_genre(f, "Folk")                  # owned but barely played
    for i in range(6):                                    # heavy techno plays
        for _ in range(10):
            store.add_history_snapshot(iid, 1.0, [f"t{i}|a"])

    bd = recommend.taste_breadth(store)
    techno_fam, folk_fam = genre_map.family("Techno"), genre_map.family("Folk")
    # play-weighting: techno dominates even though track counts are equal (6 vs 6)
    assert bd["families"][techno_fam] > 0.8
    assert bd["families"][folk_fam] < 0.2

    # palette: the heavily-played techno is "in palette" with no penalty; a far family is penalized
    pal = recommend.palette(store)
    assert pal["fit"]("Techno") == 0.0
    assert pal["fit"]("Opera") > 0.0
