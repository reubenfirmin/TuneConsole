import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import ppr


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_ppr_rank_favors_seed_neighbors():
    # line graph 0-1-2-3; restart at node 0. PPR mass concentrates at the seed and decays with distance.
    C = np.array([[0, 1, 0, 0], [1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 1, 0]], dtype=float)
    col = C.sum(axis=0); col[col == 0] = 1.0
    W = C / col
    r = ppr.ppr_rank(W, [0], alpha=0.85, iters=200)
    order = list(np.argsort(-r))
    assert order[-1] == 3                         # the farthest node holds the least PPR mass
    assert 0 in order[:2]                         # the seed sits near the top
    assert r[0] > r[2]                            # the seed outranks the two-hop node


def test_ppr_rank_empty_seed_is_zero():
    assert ppr.ppr_rank(np.eye(3), []).sum() == 0.0


def test_build_transition_empty_store(store):
    keys, W, idx = ppr.build_transition(store)
    assert keys == [] and W is None and idx == {}


def test_shadow_log_noop_without_modes(store):
    # no modes -> nothing logged, no crash
    assert ppr.shadow_log(store, now=1.0) == 0
