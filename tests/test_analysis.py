from yt_playlist.library.analysis import jaccard, find_dupes, find_overlaps

def _seed_two_playlists(store, keys_a, keys_b):
    iid = store.upsert_identity("main", "cred", None, True)
    pa = store.upsert_playlist(iid, "PLA", "A", len(keys_a), "ha", 100.0)
    pb = store.upsert_playlist(iid, "PLB", "B", len(keys_b), "hb", 100.0)
    # keys here are pre-formed identity_key strings used as both title and artist=x;
    # we pass the key as video_id surrogate to avoid unique-constraint issues across playlists.
    tids_a = [store.upsert_track(f"a{i}", k, "x", None, None) for i, k in enumerate(keys_a)]
    store.set_playlist_tracks(pa, tids_a)
    tids_b = []
    for i, k in enumerate(keys_b):
        # reuse existing track if key already inserted (shared tracks between playlists)
        existing = store.conn.execute("SELECT id FROM tracks WHERE identity_key=?", (k,)).fetchone()
        if existing:
            tids_b.append(existing[0])
        else:
            tids_b.append(store.upsert_track(f"b{i}", k, "x", None, None))
    store.set_playlist_tracks(pb, tids_b)

def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a", "b"}, {"a"}) == 0.5
    assert jaccard(set(), set()) == 0.0

def test_find_dupes_above_threshold(store):
    _seed_two_playlists(store, ["a", "b", "c", "d"], ["a", "b", "c", "x"])  # 3/5 = 0.6
    assert find_dupes(store, threshold=0.7) == []
    assert len(find_dupes(store, threshold=0.5)) == 1

def test_find_overlaps_below_dupe_threshold(store):
    _seed_two_playlists(store, ["a", "b", "c", "d"], ["a", "z", "y", "w"])  # 1 shared, jaccard 1/7
    ov = find_overlaps(store, dupe_threshold=0.7)
    assert len(ov) == 1
    assert ov[0].shared == {"a|x"}

def test_dupe_at_exact_threshold_boundary(store):
    # A={"k1","k2","k3"}, B={"k1","k2","k4"}: shared=2, union=4, jaccard=0.5
    _seed_two_playlists(store, ["k1", "k2", "k3"], ["k1", "k2", "k4"])
    threshold = 0.5
    dupes = find_dupes(store, threshold=threshold)
    assert len(dupes) == 1
    assert dupes[0].similarity == threshold
    overlaps = find_overlaps(store, dupe_threshold=threshold)
    assert overlaps == []


def test_find_overlaps_excludes_dupe_playlists(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    c = store.upsert_playlist(iid, "PLC", "C", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "S1", "X", None, 1)
    t2 = store.upsert_track("v2", "S2", "X", None, 1)
    store.set_playlist_tracks(a, [t1]); store.set_playlist_tracks(b, [t1])       # A,B identical -> dupe
    store.set_playlist_tracks(c, [t1, t2])                                       # C overlaps A and B
    ov_all = find_overlaps(store)
    assert any({o.playlist_a.id, o.playlist_b.id} & {a, b} for o in ov_all)      # without exclusion, A/B show
    ov = find_overlaps(store, exclude_playlist_ids={a, b})
    assert all(a not in (o.playlist_a.id, o.playlist_b.id) and b not in (o.playlist_a.id, o.playlist_b.id) for o in ov)


def test_find_overlaps_honors_suppressed_pairs(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Favorites", 2, "h", 1.0)
    sml = store.upsert_playlist(iid, "PLSML", "Small", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "A", "X", None, 1); t2 = store.upsert_track("v2", "B", "X", None, 1)
    store.set_playlist_tracks(fav, [t1, t2]); store.set_playlist_tracks(sml, [t1])
    assert find_overlaps(store)
    assert find_overlaps(store, suppressed={frozenset(("PLFAV", "PLSML"))}) == []


def test_empty_playlists_not_grouped_and_listed(store):
    from yt_playlist.library.analysis import find_identical_groups, find_empty_playlists
    iid = store.upsert_identity("main", "cred", None, True)
    e1 = store.upsert_playlist(iid, "PLe1", "jazz 2", 0, "h", 1.0)            # empty
    e2 = store.upsert_playlist(iid, "PLe2", "old stuff", 0, "h", 1.0)         # empty
    sys = store.upsert_playlist(iid, "SE", "Episodes for Later", 0, "h", 1.0) # system, excluded
    assert find_identical_groups(store) == []                                 # empties not clustered
    titles = [p.title for p in find_empty_playlists(store)]
    assert titles == ["jazz 2", "old stuff"]                                  # sorted, no system playlist


def test_find_near_duplicate_groups_clusters(store):
    from yt_playlist.library.analysis import find_near_duplicate_groups
    iid = store.upsert_identity("main", "cred", None, True)
    # 3 "Monkey Juice" playlists each sharing 7 of 8 tracks (pairwise ~0.78 ≥ 0.70, not identical)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(10)]
    base = [t[0], t[1], t[2], t[3], t[4], t[5], t[6]]
    x = store.upsert_playlist(iid, "PLx", "Monkey Juice", 8, "h", 1.0); store.set_playlist_tracks(x, base + [t[7]])
    y = store.upsert_playlist(iid, "PLy", "Monkey Juice", 8, "h", 1.0); store.set_playlist_tracks(y, base + [t[8]])
    z = store.upsert_playlist(iid, "PLz", "Monkey Juice", 8, "h", 1.0); store.set_playlist_tracks(z, base + [t[9]])
    groups = find_near_duplicate_groups(store)
    assert len(groups) == 1                      # one cluster, not 3 pairwise rows
    assert {p.ytm_playlist_id for p in groups[0].playlists} == {"PLx", "PLy", "PLz"}


def test_find_overlaps_kept_pair_survives_ignore(store):
    from yt_playlist.library.analysis import find_overlaps
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Favorite Songs", 3, "h", 1.0)
    other = store.upsert_playlist(iid, "PLB", "Favorite Songs 2", 2, "h", 1.0)
    noise = store.upsert_playlist(iid, "PLn", "Little Mix", 1, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(3)]
    store.set_playlist_tracks(fav, [t[0], t[1], t[2]]); store.set_playlist_tracks(other, [t[0], t[1]])
    store.set_playlist_tracks(noise, [t[0]])
    # ignore PLFAV everywhere, but keep the PLFAV–PLB pair
    ignored = {"PLFAV"}
    kept = {frozenset(("PLFAV", "PLB"))}
    pairs = {frozenset((o.playlist_a.ytm_playlist_id, o.playlist_b.ytm_playlist_id))
             for o in find_overlaps(store, ignored_ytm=ignored, kept=kept)}
    assert frozenset(("PLFAV", "PLB")) in pairs          # the pair we kept is still shown
    assert frozenset(("PLFAV", "PLn")) not in pairs       # the noise pair is muted


def test_find_tiny_playlists(store):
    from yt_playlist.library.analysis import find_tiny_playlists
    iid = store.upsert_identity("main", "cred", None, True)
    big = store.upsert_playlist(iid, "PLBIG", "Big", 5, "h", 1.0)
    one = store.upsert_playlist(iid, "PL1", "Lonely", 1, "h", 1.0)
    three = store.upsert_playlist(iid, "PL3", "Trio", 3, "h", 1.0)
    empty = store.upsert_playlist(iid, "PL0", "Empty", 0, "h", 1.0)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 2, "h", 1.0)   # system, excluded
    tracks = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(5)]
    store.set_playlist_tracks(big, tracks)
    store.set_playlist_tracks(one, tracks[:1])
    store.set_playlist_tracks(three, tracks[:3])
    store.set_playlist_tracks(lm, tracks[:2])
    got = [p.ytm_playlist_id for p in find_tiny_playlists(store)]
    assert got == ["PL1", "PL3"]   # 1 then 3 tracks; excludes big, empty, system
