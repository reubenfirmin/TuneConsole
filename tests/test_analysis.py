import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.library.analysis import jaccard, find_dupes, find_overlaps
from yt_playlist.repos.models import Playlist, PlaylistMemberships, CleanupPreferences


@pytest.fixture
def analysis_store(no_database):
    return repository_store(
        playlists={'cleanup_memberships': None},
        overlaps={'cleanup_preferences': CleanupPreferences({}, [], set(), set(), [])},
        values={'_read_only': True},
    )


def _memberships(store, rows):
    """Detached playlist/membership DTOs, without emulating repository writes."""
    playlists = [Playlist(i, 1, remote, title, len(keys), 'h', 1., 1., 1.)
                 for i, (remote, title, keys) in enumerate(rows, 1)]
    store.playlists.cleanup_memberships.return_value = PlaylistMemberships(
        playlists, {p.id: frozenset(keys) for p, (_, _, keys) in zip(playlists, rows)}, {}, set())
    return [p.id for p in playlists]

def _seed_two_playlists(store, keys_a, keys_b):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    pa = store.playlists.upsert_playlist(iid, "PLA", "A", len(keys_a), "ha", 100.0)
    pb = store.playlists.upsert_playlist(iid, "PLB", "B", len(keys_b), "hb", 100.0)
    # Shared fixture songs refer to the same exact upload in both playlists.
    tids_a = [store.tracks.upsert_track('track:'+k, k, "x", None, None) for k in keys_a]
    store.playlists.set_playlist_tracks(pa, tids_a)
    tids_b = []
    for i, k in enumerate(keys_b):
        # reuse existing track if key already inserted (shared tracks between playlists)
        existing = store.conn.execute("SELECT id FROM tracks WHERE video_id=?", ('track:'+k,)).fetchone()
        if existing:
            tids_b.append(existing[0])
        else:
            tids_b.append(store.tracks.upsert_track('track:'+k, k, "x", None, None))
    store.playlists.set_playlist_tracks(pb, tids_b)

def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a", "b"}, {"a"}) == 0.5
    assert jaccard(set(), set()) == 0.0

def test_find_dupes_above_threshold(analysis_store):
    store = analysis_store
    _memberships(store, [('PLA', 'A', {'a', 'b', 'c', 'd'}),
                         ('PLB', 'B', {'a', 'b', 'c', 'x'})])  # 3/5 = 0.6
    assert find_dupes(store, threshold=0.7) == []
    assert len(find_dupes(store, threshold=0.5)) == 1

def test_find_overlaps_below_dupe_threshold(store):
    _seed_two_playlists(store, ["a", "b", "c", "d"], ["a", "z", "y", "w"])  # 1 shared, jaccard 1/7
    ov = find_overlaps(store, dupe_threshold=0.7)
    assert len(ov) == 1
    assert ov[0].shared == {"a|x"}

def test_dupe_at_exact_threshold_boundary(analysis_store):
    # A={"k1","k2","k3"}, B={"k1","k2","k4"}: shared=2, union=4, jaccard=0.5
    store = analysis_store
    _memberships(store, [('PLA', 'A', {'k1', 'k2', 'k3'}), ('PLB', 'B', {'k1', 'k2', 'k4'})])
    threshold = 0.5
    dupes = find_dupes(store, threshold=threshold)
    assert len(dupes) == 1
    assert dupes[0].similarity == threshold
    overlaps = find_overlaps(store, dupe_threshold=threshold)
    assert overlaps == []


def test_find_overlaps_excludes_dupe_playlists(analysis_store):
    store = analysis_store
    a, b, c = _memberships(store, [('PLA', 'A', {'s1|x'}), ('PLB', 'B', {'s1|x'}),
                                 ('PLC', 'C', {'s1|x', 's2|x'})])
    ov_all = find_overlaps(store)
    assert any({o.playlist_a.id, o.playlist_b.id} & {a, b} for o in ov_all)      # without exclusion, A/B show
    ov = find_overlaps(store, exclude_playlist_ids={a, b})
    assert all(a not in (o.playlist_a.id, o.playlist_b.id) and b not in (o.playlist_a.id, o.playlist_b.id) for o in ov)


def test_find_overlaps_honors_suppressed_pairs(analysis_store):
    store = analysis_store
    _memberships(store, [('PLFAV', 'Favorites', {'a|x', 'b|x'}), ('PLSML', 'Small', {'a|x'})])
    assert find_overlaps(store)
    assert find_overlaps(store, suppressed={frozenset(("PLFAV", "PLSML"))}) == []


def test_empty_playlists_not_grouped_and_listed(analysis_store):
    from yt_playlist.library.analysis import find_identical_groups, find_empty_playlists
    store = analysis_store
    _memberships(store, [('PLe1', 'jazz 2', set()), ('PLe2', 'old stuff', set()),
                         ('SE', 'Episodes for Later', set())])
    assert find_identical_groups(store) == []                                 # empties not clustered
    titles = [p.title for p in find_empty_playlists(store)]
    assert titles == ["jazz 2", "old stuff"]                                  # sorted, no system playlist


def test_find_near_duplicate_groups_clusters_and_honor_merge_ignore(analysis_store):
    from yt_playlist.library.analysis import find_near_duplicate_groups, merge_signature
    store = analysis_store
    # 3 "Monkey Juice" playlists each sharing 7 of 8 tracks (pairwise ~0.78 ≥ 0.70, not identical)
    base = {f's{i}|x' for i in range(7)}
    _memberships(store, [('PLx', 'Monkey Juice', base | {'s7|x'}),
                         ('PLy', 'Monkey Juice', base | {'s8|x'}),
                         ('PLz', 'Monkey Juice', base | {'s9|x'})])
    groups = find_near_duplicate_groups(store)
    assert len(groups) == 1                      # one cluster, not 3 pairwise rows
    assert {p.provider_playlist_id for p in groups[0].playlists} == {"PLx", "PLy", "PLz"}
    sig = merge_signature(["PLx", "PLy", "PLz"])
    assert find_near_duplicate_groups(store, ignored_sigs={sig}) == []


def test_near_duplicate_pairs_only_include_similar_edges(analysis_store):
    from yt_playlist.library.analysis import find_near_duplicate_groups, near_duplicate_pairs
    store = analysis_store
    tracks = [f's{i}|x' for i in range(10)]
    # A-B and B-C are 80% similar; A-C is only 60%, despite sharing a connected group.
    _memberships(store, [('PLA', 'PLA', tracks[:8]), ('PLB', 'PLB', tracks), ('PLC', 'PLC', tracks[2:])])
    groups = find_near_duplicate_groups(store)
    assert len(groups) == 1
    pairs = near_duplicate_pairs(store, groups)
    assert {frozenset((p.playlist_a.provider_playlist_id, p.playlist_b.provider_playlist_id))
            for p in pairs} == {frozenset(("PLA", "PLB")), frozenset(("PLB", "PLC"))}


def test_find_overlaps_kept_pair_survives_ignore(analysis_store):
    from yt_playlist.library.analysis import find_overlaps
    store = analysis_store
    _memberships(store, [('PLFAV', 'Favorite Songs', {'s0|x', 's1|x', 's2|x'}),
                         ('PLB', 'Favorite Songs 2', {'s0|x', 's1|x'}),
                         ('PLn', 'Little Mix', {'s0|x'})])
    # ignore PLFAV everywhere, but keep the PLFAV–PLB pair
    ignored = {"PLFAV"}
    kept = {frozenset(("PLFAV", "PLB"))}
    pairs = {frozenset((o.playlist_a.provider_playlist_id, o.playlist_b.provider_playlist_id))
             for o in find_overlaps(store, ignored_ytm=ignored, kept=kept)}
    assert frozenset(("PLFAV", "PLB")) in pairs          # the pair we kept is still shown
    assert frozenset(("PLFAV", "PLn")) not in pairs       # the noise pair is muted


def test_find_tiny_playlists(analysis_store):
    from yt_playlist.library.analysis import find_tiny_playlists
    store = analysis_store
    tracks = [f's{i}|x' for i in range(5)]
    _memberships(store, [('PLBIG', 'Big', tracks), ('PL1', 'Lonely', tracks[:1]),
                         ('PL3', 'Trio', tracks[:3]), ('PL0', 'Empty', []),
                         ('LM', 'Liked Music', tracks[:2])])
    got = [p.provider_playlist_id for p in find_tiny_playlists(store)]
    assert got == ["PL1", "PL3"]   # 1 then 3 tracks; excludes big, empty, system


def test_empty_and_tiny_honor_category_ignore(analysis_store):
    from yt_playlist.library.analysis import find_empty_playlists, find_tiny_playlists
    store = analysis_store
    _memberships(store, [('PLe', 'empty one', set()), ('PL1', 'lonely', {'s0|x'})])
    assert [p.provider_playlist_id for p in find_empty_playlists(store, ignored={"PLe"})] == []
    assert [p.provider_playlist_id for p in find_tiny_playlists(store, ignored={"PL1"})] == []
    # category-scoped: ignoring as 'empty' must NOT suppress it from the tiny list (and vice-versa)
    assert [p.provider_playlist_id for p in find_tiny_playlists(store, ignored={"PLe"})] == ["PL1"]


def test_merge_signature_is_canonical():
    from yt_playlist.library.analysis import merge_signature
    assert merge_signature(["B", "A", "C"]) == merge_signature(["C", "B", "A"]) == "A|B|C"


def test_ignored_merge_reappears_when_membership_changes(store):
    """The dismissal is keyed to the exact set: a NEW copy joining the cluster re-surfaces it."""
    from yt_playlist.library.analysis import find_identical_groups, merge_signature
    iid = store.accounts.upsert_account("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "S1", "X", None, 1)
    store.playlists.set_playlist_tracks(a, [t]); store.playlists.set_playlist_tracks(b, [t])
    assert len(find_identical_groups(store)) == 1
    ignored = {merge_signature(["PLA", "PLB"])}
    assert find_identical_groups(store, ignored_sigs=ignored) == []
    c = store.playlists.upsert_playlist(iid, "PLC", "Mix", 1, "h", 1.0)              # a third identical copy appears
    store.playlists.set_playlist_tracks(c, [t])
    groups = find_identical_groups(store, ignored_sigs=ignored)            # {A,B,C} != {A,B} -> reappears
    assert len(groups) == 1 and {p.provider_playlist_id for p in groups[0].playlists} == {"PLA", "PLB", "PLC"}
