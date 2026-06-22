"""DAO suite for OverlapRepo (split out of the Store god class)."""


def test_suppress_and_unsuppress(store):
    store.overlaps.suppress_overlap("PLB", "PLA", now=1.0)        # order is normalized to (a,b)
    assert store.overlaps.get_suppressed_overlap_pairs() == {frozenset(("PLA", "PLB"))}
    assert store.overlaps.get_suppressed_overlaps() == [("PLA", "PLB", 1.0)]
    store.overlaps.unsuppress_overlap("PLA", "PLB")
    assert store.overlaps.get_suppressed_overlap_pairs() == set()


def test_ignore_playlist(store):
    store.overlaps.ignore_overlap_playlist("PLX", now=2.0)
    assert store.overlaps.get_overlap_ignored() == {"PLX"}
    store.overlaps.unignore_overlap_playlist("PLX")
    assert store.overlaps.get_overlap_ignored() == set()


def test_kept_pair_is_unordered(store):
    store.overlaps.keep_overlap_pair("PLB", "PLA", now=2.0)
    assert store.overlaps.get_overlap_kept_pairs() == {frozenset(("PLA", "PLB"))}


def test_store_facade_delegates_legacy_calls(store):
    # Pre-split call sites use store.x() directly; __getattr__ must route them to the DAO.
    store.suppress_overlap("PLA", "PLB", 1.0)
    assert store.get_suppressed_overlap_pairs() == {frozenset(("PLA", "PLB"))}
    assert store.get_suppressed_overlap_pairs() == store.overlaps.get_suppressed_overlap_pairs()
