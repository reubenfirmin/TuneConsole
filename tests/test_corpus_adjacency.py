from yt_playlist.rec import genre_map, recommend


def test_corpus_pulls_cooccurring_genres_closer(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Techno + Hard Rock are unrelated in the static map (distance 1.0), but here they're
    # always playlisted together -> the blended distance should drop below 1.0.
    for n in range(3):
        a = store.upsert_track(f"a{n}", f"A{n}", "X", None, None); store.set_track_genre(a, "Techno")
        b = store.upsert_track(f"b{n}", f"B{n}", "Y", None, None); store.set_track_genre(b, "Hard Rock")
        p = store.upsert_playlist(iid, f"P{n}", f"P{n}", 2, "h", 0.0)
        store.set_playlist_tracks(p, [a, b])

    dist = recommend.genre_distance_fn(store)
    assert genre_map.distance("Techno", "Hard Rock") == 1.0      # static says unrelated
    assert dist("Techno", "Hard Rock") < 1.0                     # corpus says you pair them


def test_corpus_falls_back_to_static_for_ungrouped(store):
    store.upsert_identity("main", "cred", None, True)
    dist = recommend.genre_distance_fn(store)
    assert dist("Trance", "Tech Trance") == genre_map.distance("Trance", "Tech Trance")
