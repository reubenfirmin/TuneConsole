"""#86 discovery ranking: bridge strength ranks; proxy taste only gates. The old taste*strength
product double-counted anchors (the proxy IS the anchors' weighted average)."""
import numpy as np
import pytest

from yt_playlist.rec.discover import _rank_candidates
from yt_playlist.rec.taste_context import PlaylistTaste


@pytest.fixture
def taste():
    return PlaylistTaste(["Playlist A"], np.array([[1., 0.]]), np.ones(1))


def _bridge(weight, direction):
    v = np.array(direction, dtype=np.float64)
    return (v / np.linalg.norm(v), weight, "anchor")


def test_score_is_strength_not_taste_times_strength(taste):
    # One strong on-taste edge vs two weaker edges summing higher: strength must decide.
    bridges = {"weak_single": [_bridge(1.0, [1, 0])],
               "strong_multi": [_bridge(0.8, [1, 0]), _bridge(0.7, [1, 0.1])]}
    out = {c["cand"]: c for c in _rank_candidates(bridges, taste, 2)}
    assert out["strong_multi"]["score"] > out["weak_single"]["score"]
    assert out["weak_single"]["score"] == 1.0          # strength, no taste factor


def test_off_taste_candidate_is_gated_out(taste):
    bridges = {"off": [_bridge(5.0, [-1, 0])]}          # points away from taste
    assert _rank_candidates(bridges, taste, 2) == []
    # Preserve the signed gate: a small positive match cannot hide a larger negative fit.
    opposed = PlaylistTaste(["Positive", "Negative"], np.array([[1., 0.], [-1., 0.]]),
                            np.array([.25, .75]))
    assert _rank_candidates({"cancelled": [_bridge(5., [1., 0.])]}, opposed, 2) == []
    empty = PlaylistTaste([], np.empty((0, 2)), np.zeros(0))
    assert _rank_candidates({"unknown": [_bridge(5., [1., 0.])]}, empty, 2) == []


def test_because_names_top_anchors_and_fits_pass_through(taste):
    v = np.array([1.0, 0.0])
    bridges = {"c": [(v, 0.2, "minor"), (v, 0.9, "major")]}
    (item,) = _rank_candidates(bridges, taste, 2)
    assert item["because"][0] == "major" and item["fits"] == ["Playlist A"]
    contexts = PlaylistTaste(["Small", "Largest", "Second", "Third", "Opposed"],
                             np.array([[1., 0.]] * 4 + [[-1., 0.]]),
                             np.array([.05, .4, .3, .2, .05]))
    (item,) = _rank_candidates(bridges, contexts, 2)
    assert item["fits"] == ["Largest", "Second", "Third"]
