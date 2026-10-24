import numpy as np
import pytest
import tracemalloc
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


def test_batched_assignments_use_euclidean_distance_and_first_tie(monkeypatch):
    monkeypatch.setattr(tm, '_DISTANCE_WORK_BYTES', 32)
    X = np.array([[1., 0.], [0., 1.], [.5, .5], [0., 0.]])
    centers = np.array([[.2, 0.], [2., 0.], [0., 0.]])
    expected = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
    np.testing.assert_array_equal(tm._nearest_centers(X, centers), expected.argmin(axis=1))
    # Dot-product ranking would incorrectly pick the longer centroid for the first row.
    assert tm._nearest_centers(X[:1], centers)[0] == 0
    np.testing.assert_array_equal(tm._nearest_centers(X, centers[[0, 0]]), np.zeros(4, dtype=int))


@pytest.mark.parametrize('seed', [1, 7, 1234567])
@pytest.mark.parametrize('degenerate', [False, True])
def test_chunking_preserves_seeding_labels_centroids_and_empty_clusters(monkeypatch, seed, degenerate):
    X = _blobs()
    if degenerate:
        X[:] = X[0]    # duplicate centers, exact ties, and empty clusters
    monkeypatch.setattr(tm, '_DISTANCE_WORK_BYTES', 512)
    labels, centers = tm._kmeanspp(X, 4, seed)

    def full_tensor(X, centers):
        yield 0, ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)

    monkeypatch.setattr(tm, '_distance_batches', full_tensor)
    reference_labels, reference_centers = tm._kmeanspp(X, 4, seed)
    np.testing.assert_array_equal(labels, reference_labels)
    np.testing.assert_array_equal(centers, reference_centers)


def test_distance_chunks_check_cancellation(monkeypatch):
    monkeypatch.setattr(tm, '_DISTANCE_WORK_BYTES', 64)
    calls = []

    def stop():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError('cancelled')

    monkeypatch.setattr(tm, 'check_stopping', stop)
    with pytest.raises(RuntimeError, match='cancelled'):
        tm._nearest_centers(np.ones((100, 8)), np.ones((4, 8)))
    assert len(calls) == 2


def test_large_assignment_bounds_temporary_allocation():
    rng = np.random.default_rng(42)
    X = rng.normal(size=(10_000, 256))
    centers = rng.normal(size=(40, 256))
    # The former 10,000 x 40 x 256 float64 difference tensor alone used 819,200,000 bytes.
    # Track NumPy allocations after creating inputs, so this measures working memory.
    tracemalloc.start()
    try:
        labels = tm._nearest_centers(X, centers)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert labels.shape == (len(X),)
    assert peak < 2 * tm._DISTANCE_WORK_BYTES + labels.nbytes
