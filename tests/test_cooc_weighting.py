"""#38 §4c: playcount/recency-weighted co-occurrence. The embedding's co-occurrence matrix can scale
each track-pair by a per-track gain, so heavily-played combinations dominate over stale ones, while
the default (no weights) stays the binary-membership model."""
import numpy as np

from yt_playlist.rec import embed


def test_cooc_weighting_scales_pair_contributions():
    keys = ["a", "b", "c"]
    baskets = [["a", "b"], ["b", "c"]]
    base = embed._cooc_matrix(baskets, keys)                       # binary membership
    w = embed._cooc_matrix(baskets, keys, weights={"b": 2.0})      # b's pairings count double
    ia, ib, ic = 0, 1, 2
    assert w[ia, ib] == base[ia, ib] * 2.0                         # pair (a,b): gain 1*2
    assert w[ib, ic] == base[ib, ic] * 2.0                         # pair (b,c): gain 2*1
    assert base[ia, ic] == 0.0 and w[ia, ic] == 0.0               # a and c never co-occur
    assert np.allclose(w, w.T)                                     # stays symmetric


def test_cooc_weighting_off_matches_binary():
    keys = ["a", "b", "c"]
    baskets = [["a", "b", "c"]]
    assert np.array_equal(embed._cooc_matrix(baskets, keys),
                          embed._cooc_matrix(baskets, keys, weights=None))


def test_cooc_weights_grow_with_playcount(monkeypatch):
    # The per-track gain rises with play frequency; an unplayed track keeps the binary baseline (1.0).
    class _S:
        def play_counts(self):
            return {"hot": 99, "warm": 3, "cold": 0}
    w = embed._cooc_weights(_S())
    assert w.get("hot", 1.0) > w.get("warm", 1.0) > 1.0           # more plays -> larger gain
    assert w.get("cold", 1.0) == 1.0                              # never played -> neutral, never erased
