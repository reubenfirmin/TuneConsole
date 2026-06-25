"""embed.cluster_expand: the seeded, push-away ring used by the Clusters canvas.

A node's next ring = library tracks nearest the centroid of its PINNED path, minus a push-away
tilt toward the centroid of the PRUNED ("negative model") set. Geometry is injected directly as
2D unit vectors so the ranking math is exercised deterministically, independent of the SVD build.
"""
import math

import numpy as np

from yt_playlist.rec import embed


def _put(store, vecs):
    """vecs = {identity_key: (x, y)}; stored L2-normalised as float32, like the real model."""
    rows = []
    for k, xy in vecs.items():
        v = np.asarray(xy, dtype=np.float32)
        v /= np.linalg.norm(v) + 1e-9
        rows.append((k, v.tobytes()))
    store.replace_rec_vectors(rows)


def _unit(deg):
    r = math.radians(deg)
    return (math.cos(r), math.sin(r))


def test_ranks_by_positive_centroid(store):
    # p points along 0°; a/b sit near it, z is opposite. Nearest-to-centroid order, seed excluded.
    _put(store, {"p": _unit(0), "a": _unit(15), "b": _unit(40), "z": _unit(180)})
    out = embed.cluster_expand(store, pos_keys=["p"], neg_keys=[], topn=3)
    keys = [k for k, _ in out]
    assert "p" not in keys                     # the seed itself is never re-offered
    assert keys[:2] == ["a", "b"]              # closest-first
    assert keys[-1] == "z"                     # the opposite pole ranks last


def test_pushes_away_from_negative(store):
    # a and b are symmetric about p (equal positive score). A pruned seed n sits right on top of a,
    # so push-away must demote a below b: the whole point of "prune as negative signal".
    _put(store, {"p": _unit(0), "a": _unit(20), "b": _unit(-20), "n": _unit(18)})
    plain = [k for k, _ in embed.cluster_expand(
        store, pos_keys=["p"], neg_keys=[], exclude=["n"], topn=2)]
    assert set(plain) == {"a", "b"}            # without a negative, a/b tie near the top

    pushed = [k for k, _ in embed.cluster_expand(store, pos_keys=["p"], neg_keys=["n"], topn=2)]
    assert pushed[0] == "b"                     # b (far from the pruned region) now outranks a
    assert pushed.index("b") < pushed.index("a")


def test_excludes_seeds_pruned_and_extra(store):
    _put(store, {"p": _unit(0), "a": _unit(10), "n": _unit(30), "x": _unit(50), "y": _unit(70)})
    out = [k for k, _ in embed.cluster_expand(
        store, pos_keys=["p"], neg_keys=["n"], exclude=["x"], topn=10)]
    assert "p" not in out and "n" not in out    # positive + negative seeds suppressed
    assert "x" not in out                        # already-on-canvas keys suppressed
    assert "a" in out and "y" in out


def test_empty_before_build(store):
    assert embed.cluster_expand(store, pos_keys=["p"], neg_keys=[]) == []
