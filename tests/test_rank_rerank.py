"""#86 reranking on a rank base: multipliers act consistently regardless of pool composition."""
from yt_playlist.rec.scoring import axis_adjusted_scores, genre_adjusted_scores


def _order(d):
    return sorted(d, key=lambda k: -d[k])


def test_neutral_weights_are_a_pure_noop():
    scores = {"a": -0.2, "b": 0.4}
    assert axis_adjusted_scores(scores, {"a": 1.0, "b": 1.0}) is scores
    assert genre_adjusted_scores(scores, {"a": "g"}, {"g": 1.0}) is scores


def test_mute_sinks_to_zero_and_boost_rises():
    scores = {"a": 0.9, "b": 0.1}
    out = axis_adjusted_scores(scores, {"a": 0.0, "b": 2.0})
    assert out["a"] == 0.0 and out["b"] > 0.0


def test_rerank_is_independent_of_pool_junk():
    # The old shift-by-min base failed this: adding a terrible candidate changed how far a
    # multiplier could lift a low scorer relative to a high one. mult=2.2 clears the crossover
    # point (2.0) for this rank/2 vs rank/3 comparison with margin on both sides, so the outcome
    # (lo overtakes hi) is decided by the multiplier, not by how much junk happens to be in the pool.
    mult = {"lo": 2.2, "hi": 1.0}
    base = {"lo": 0.10, "hi": 0.80}
    with_junk = dict(base, junk=-5.0)
    o1 = _order(axis_adjusted_scores(base, mult))
    o2 = [k for k in _order(axis_adjusted_scores(with_junk, dict(mult, junk=1.0))) if k != "junk"]
    assert o1 == o2


def test_genre_weights_apply_by_family():
    scores = {"a": 0.9, "b": 0.5}
    out = genre_adjusted_scores(scores, {"a": "rock", "b": "jazz"}, {"rock": 0.0, "jazz": 1.0})
    assert out["a"] == 0.0 and out["b"] > 0.0
