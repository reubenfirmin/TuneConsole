from yt_playlist import recommend


def _tag(store, iid, n, genre, prefix):
    for i in range(n):
        t = store.upsert_track(f"{prefix}{i}", f"{prefix}{i}", "A", None, None)
        store.set_track_genre(t, genre)


def test_narrow_library_has_low_breadth(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _tag(store, iid, 10, "Techno", "t")          # one family only
    assert recommend.taste_breadth(store)["breadth"] == 0.0


def test_eclectic_library_has_high_breadth(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for g, pfx in [("Techno", "t"), ("Hard Rock", "r"), ("Jazz", "j"), ("Hip Hop", "h")]:
        _tag(store, iid, 5, g, pfx)              # four very different families, even mix
    assert recommend.taste_breadth(store)["breadth"] > 0.9


def test_palette_penalizes_absent_family_more_when_broad(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for g, pfx in [("Techno", "t"), ("Jazz", "j"), ("Hip Hop", "h"), ("Folk", "f")]:
        _tag(store, iid, 5, g, pfx)
    pal = recommend.palette(store)
    assert pal["fit"]("Techno") == 0.0           # in palette -> no penalty
    assert pal["fit"]("Opera") > 0.0             # absent family -> penalized


def test_breadth_zero_when_untagged(store):
    store.upsert_identity("main", "cred", None, True)
    store.upsert_track("x", "X", "A", None, None)   # no genre
    assert recommend.taste_breadth(store)["breadth"] == 0.0
