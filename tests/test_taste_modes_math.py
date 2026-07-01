import numpy as np
from yt_playlist.rec import taste_modes as tm


def _blobs():
    rng = np.random.Generator(np.random.PCG64(42))
    a = rng.normal([5, 0, 0], 0.05, size=(40, 3))
    b = rng.normal([0, 5, 0], 0.05, size=(40, 3))
    c = rng.normal([0, 0, 5], 0.05, size=(40, 3))
    X = np.vstack([a, b, c]).astype(np.float64)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X


def test_deterministic_for_fixed_seed():
    X = _blobs()
    l1, c1 = tm._kmeanspp(X, 3, seed=7)
    l2, c2 = tm._kmeanspp(X, 3, seed=7)
    assert np.array_equal(l1, l2)
    assert np.allclose(c1, c2)


def test_recovers_planted_clusters():
    X = _blobs()
    labels, centroids = tm._kmeanspp(X, 3, seed=7)
    # Each planted block of 40 must land in a single cluster.
    for block in (slice(0, 40), slice(40, 80), slice(80, 120)):
        assert len(set(labels[block].tolist())) == 1
    # And the three blocks must occupy three different clusters.
    assert len({labels[0], labels[40], labels[80]}) == 3
