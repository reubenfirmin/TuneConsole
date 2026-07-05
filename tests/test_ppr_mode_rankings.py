import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import ppr, embed


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_ppr_rank_tol_early_stops_at_fixed_point():
    # 3-node path graph a-b-c.  C (symmetric adjacency):
    #   C = [[0,1,0],[1,0,1],[0,1,0]]  -> column sums [1,2,1]
    #   W = C / col = [[0,0.5,0],[1,0,1],[0,0.5,0]]
    # Seed node 0 (a), alpha=0.5, p=[1,0,0].  Fixed point of r=(1-a)p + aWr:
    #   x = 0.5 + 0.25y ; y = 0.5(x+z) ; z = 0.25y  ->  0.75y = 0.25 -> y = 1/3,
    #   x = 0.5 + 1/12 = 7/12, z = 1/12.  r* = [7/12, 1/3, 1/12] (sums to 1; PPR conserves mass).
    W = np.array([[0.0, 0.5, 0.0], [1.0, 0.0, 1.0], [0.0, 0.5, 0.0]])
    r = ppr.ppr_rank(W, [0], alpha=0.5, iters=500, tol=1e-12)
    assert np.allclose(r, [7 / 12, 1 / 3, 1 / 12], atol=1e-6)
    # tol=0 with a tight cap must reach the same fixed point (500 iters is far past convergence).
    r2 = ppr.ppr_rank(W, [0], alpha=0.5, iters=500, tol=0.0)
    assert np.allclose(r2, [7 / 12, 1 / 3, 1 / 12], atol=1e-6)


def test_mode_rankings_orders_by_per_mode_ppr(monkeypatch, store):
    # Graph = the same a-b-c path (W above).  keys index: a=0, b=1, c=2.
    keys = ["a", "b", "c"]
    W = np.array([[0.0, 0.5, 0.0], [1.0, 0.0, 1.0], [0.0, 0.5, 0.0]])
    idx = {"a": 0, "b": 1, "c": 2}
    monkeypatch.setattr(ppr, "build_transition", lambda s: (keys, W, idx))
    # Content vectors: a,b sit in mode 1 (centroid e0), c sits in mode 2 (centroid e1).
    lkeys = ["a", "b", "c"]
    LV = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    lidx = {"a": 0, "b": 1, "c": 2}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (lkeys, LV, lidx))
    store.modes.replace_modes([
        {"mode_id": 1, "label": "m1", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
        {"mode_id": 2, "label": "m2", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 40, "rep_keys": []},
    ], retired_ids=[], now=1.0)

    r = ppr.mode_rankings(store, alpha=0.5, iters=500, tol=1e-12, depth=10)
    # Mode 1 members = {a,b}, seed idx [0,1], p=[0.5,0.5,0].  Fixed point:
    #   x = 0.25 + 0.25y ; y = 0.25 + 0.5(x+z) ; z = 0.25y  ->  0.75y = 0.375 -> y = 0.5,
    #   x = 0.375, z = 0.125.  r=[0.375, 0.5, 0.125] -> desc order [b, a, c].
    assert r[1] == ["b", "a", "c"]
    # Mode 2 members = {c}, seed idx [2], p=[0,0,1].  Fixed point:
    #   x = 0.25y ; y = 0.5(x+z) ; z = 0.5 + 0.25y  ->  0.75y = 0.25 -> y = 1/3,
    #   x = 1/12, z = 7/12.  r=[1/12, 1/3, 7/12] -> desc order [c, b, a].
    assert r[2] == ["c", "b", "a"]


def test_mode_rankings_empty_without_modes(store):
    assert ppr.mode_rankings(store) == {}


def test_mode_rankings_stale_centroid_dim_returns_empty(monkeypatch, store):
    # Stale-modes window (content space rebuilt at a new dim before the mode rebuild): {} rather
    # than a matmul shape crash in the rec worker.
    keys = ["a", "b", "c"]
    W = np.array([[0.0, 0.5, 0.0], [1.0, 0.0, 1.0], [0.0, 0.5, 0.0]])
    idx = {"a": 0, "b": 1, "c": 2}
    monkeypatch.setattr(ppr, "build_transition", lambda s: (keys, W, idx))
    lkeys = ["a", "b", "c"]
    LV = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)   # 3-D
    lidx = {"a": 0, "b": 1, "c": 2}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (lkeys, LV, lidx))
    store.modes.replace_modes([
        {"mode_id": 1, "label": "m1", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},   # 2-D
    ], retired_ids=[], now=1.0)

    assert ppr.mode_rankings(store, alpha=0.5, iters=50, tol=0.0, depth=10) == {}
