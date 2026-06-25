"""#21: app-generated (quarantined) playlists are excluded from every Cleanup category.

A generated playlist is built FROM your library, so it trivially overlaps/duplicates its source
playlists, and it's app-managed, not yours to tidy. So it must never surface as an overlap, dupe,
near-dup, identical-group member, empty, or tiny candidate, nor in the cleanup summary.
"""
from yt_playlist.library import analysis
from yt_playlist.repos.rec_query import GENERATED_GROUP


def _pl(store, iid, ytm, title, track_ids, *, generated=False):
    pid = store.upsert_playlist(iid, ytm, title, len(track_ids), "h", 1.0)
    store.set_playlist_tracks(pid, track_ids)
    if generated:
        store.set_playlist_group(ytm, GENERATED_GROUP)
    return pid


def _seed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "S1", "X", None, 1)
    t2 = store.upsert_track("v2", "S2", "X", None, 1)
    t3 = store.upsert_track("v3", "S3", "X", None, 1)
    return iid, (t1, t2, t3)


def test_generated_excluded_from_overlaps(store):
    iid, (t1, t2, t3) = _seed(store)
    a = _pl(store, iid, "PLA", "User A", [t1, t2, t3])
    g = _pl(store, iid, "PLGEN", "Gen mix", [t1, t3], generated=True)   # shares with A -> would overlap
    ov = analysis.find_overlaps(store)
    assert all(g not in (o.playlist_a.id, o.playlist_b.id) for o in ov)


def test_generated_excluded_from_dupes_and_identical_groups(store):
    iid, (t1, t2, t3) = _seed(store)
    a = _pl(store, iid, "PLA", "User A", [t1, t2, t3])
    g = _pl(store, iid, "PLGEN", "Gen mix", [t1, t2, t3], generated=True)  # identical to A
    assert all(g not in (d.playlist_a.id, d.playlist_b.id) for d in analysis.find_dupes(store))
    groups = analysis.find_identical_groups(store)
    assert all(g not in {p.id for p in grp.playlists} for grp in groups)
    # A alone is no longer a duplicate of anything once G is excluded
    assert groups == []


def test_generated_excluded_from_near_dupes(store):
    iid, (t1, t2, t3) = _seed(store)
    t4 = store.upsert_track("v4", "S4", "X", None, 1)
    a = _pl(store, iid, "PLA", "User A", [t1, t2, t3, t4])
    g = _pl(store, iid, "PLGEN", "Gen mix", [t1, t2, t3], generated=True)   # ~0.75 similar, not identical
    near = analysis.find_near_duplicate_groups(store)
    assert all(g not in {p.id for p in grp.playlists} for grp in near)


def test_generated_excluded_from_empty_and_tiny(store):
    iid, (t1, t2, t3) = _seed(store)
    ge = _pl(store, iid, "PLGE", "Gen empty", [], generated=True)
    gt = _pl(store, iid, "PLGT", "Gen tiny", [t1], generated=True)
    assert all(p.id != ge for p in analysis.find_empty_playlists(store))
    assert all(p.id != gt for p in analysis.find_tiny_playlists(store))


def test_generated_excluded_from_cleanup_summary(store):
    iid, (t1, t2, t3) = _seed(store)
    a = _pl(store, iid, "PLA", "User A", [t1, t2, t3])
    g = _pl(store, iid, "PLGEN", "Gen mix", [t1, t2, t3], generated=True)
    summary = analysis.cleanup_summary(store)
    assert all(p.id != g for p in summary.playlists)


def test_user_playlists_still_detected(store):
    """Sanity: the exclusion doesn't suppress genuine user-vs-user overlaps."""
    iid, (t1, t2, t3) = _seed(store)
    a = _pl(store, iid, "PLA", "User A", [t1, t2, t3])
    b = _pl(store, iid, "PLB", "User B", [t1, t3])      # overlaps A, both user playlists
    ov = analysis.find_overlaps(store)
    assert any({a, b} == {o.playlist_a.id, o.playlist_b.id} for o in ov)
