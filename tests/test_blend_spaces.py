"""#86 cross-space blending on comparable scales: a single-space candidate competes on its rank in
its own space, not on a raw cosine against blended values."""
from yt_playlist.rec.embed import _blend_spaces


def test_single_space_candidate_competes_on_rank():
    # Old math: content-only "x" kept raw 0.30 while both-space keys blended toward higher collab
    # cosines; a top-of-its-space content candidate could never win. New math: x is rank 1.0 in
    # content and must beat a mid-rank blended key at w=0.5.
    collab = {"a": 0.9, "b": 0.5}
    content = {"a": 0.2, "x": 0.3}
    out = _blend_spaces(collab, content, 0.5)
    assert out["x"] > out["b"]


def test_w_zero_is_pure_collab_order():
    collab = {"a": 0.9, "b": 0.5}
    content = {"a": 0.1, "b": 0.99}
    out = _blend_spaces(collab, content, 0.0)
    assert out["a"] > out["b"]


def test_both_spaces_blend_monotonically():
    collab = {"a": 0.9, "b": 0.1}
    content = {"a": 0.1, "b": 0.9}
    lo, hi = _blend_spaces(collab, content, 0.2), _blend_spaces(collab, content, 0.8)
    assert lo["a"] > lo["b"] and hi["b"] > hi["a"]
