"""Tests for the pure discovery-pool bounding/rotation policy (#52 pool explosion fix)."""
import random

from yt_playlist.rec import discovery_pool as dp


def _cat(*ids):
    return [{"browse_id": b, "title": b, "year": "2000", "thumbnail": None} for b in ids]


def test_choose_keep_prefers_unshown_and_caps():
    pooled = {"a": 0, "b": 0, "c": 3, "d": 5}        # a,b unshown; c,d shown
    keep = dp.choose_album_keep(pooled, n=2, rng=random.Random(0))
    assert keep == {"a", "b"}                          # unshown retained, shown dropped first


def test_choose_keep_random_among_unshown_when_over_cap():
    pooled = {b: 0 for b in "abcdef"}                  # all unshown, more than n
    keep = dp.choose_album_keep(pooled, n=3, rng=random.Random(1))
    assert len(keep) == 3 and keep <= set("abcdef")


def test_rotate_fills_from_whole_catalog_not_newest():
    # catalog has old + new; with an empty pool the fill is a random draw across ALL of it.
    catalog = _cat("old1", "old2", "mid1", "new1", "new2")
    keep, add = dp.rotate_album_sample(catalog, pooled={}, n=3, rng=random.Random(7))
    assert keep == set()
    assert len(add) == 3 and all(a in catalog for a in add)


def test_rotate_retains_unshown_and_rotates_out_shown():
    catalog = _cat("a", "b", "c", "d", "e")
    pooled = {"a": 0, "b": 2}                          # a unshown (retain), b shown (rotate out)
    keep, add = dp.rotate_album_sample(catalog, pooled, n=3, rng=random.Random(3))
    assert "a" in keep and "b" not in keep            # b rotated out
    assert len(keep) + len(add) == 3
    assert all(a["browse_id"] not in pooled for a in add)   # fills are not already pooled
