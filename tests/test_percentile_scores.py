"""#86 rank/percentile normalizer: the pool-independent base that replaced shift-by-min."""
from yt_playlist.rec.embed import percentile_scores


def test_maps_to_average_ranks():
    out = percentile_scores({"a": -0.5, "b": 0.1, "c": 0.9})
    assert out == {"a": 1 / 3, "b": 2 / 3, "c": 1.0}


def test_ties_share_average_rank():
    out = percentile_scores({"a": 0.5, "b": 0.5, "c": 1.0, "d": 0.1})
    assert out["d"] == 1 / 4 and out["c"] == 1.0
    assert out["a"] == out["b"] == 2.5 / 4          # ranks 2 and 3 averaged


def test_pool_size_independent_relative_order():
    small = percentile_scores({"a": 0.2, "b": 0.8})
    big = percentile_scores({"a": 0.2, "b": 0.8, "junk": -9.0})
    assert (small["b"] > small["a"]) and (big["b"] > big["a"])
    assert big["junk"] == 1 / 3


def test_empty_and_single():
    assert percentile_scores({}) == {}
    assert percentile_scores({"a": -3.0}) == {"a": 1.0}
