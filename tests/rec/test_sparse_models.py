"""Compare sparse production calculations with the former dense math on fixed inputs."""
from collections import Counter
from itertools import islice, permutations
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.sparse import csc_array, csr_array, issparse

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.rec import embedding_training as training, ppr
from yt_playlist.rec.pair_stream import PairStream, _shuffled_indices


def fixture_baskets(n=96):
    rng = np.random.default_rng(115)
    keys = [f"track-{i:04d}" for i in range(n)]
    baskets = [rng.choice(keys, size=int(rng.integers(3, min(15, n + 1))), replace=False).tolist()
               for _ in range(n * 2)]
    return keys, baskets


def dense_cooc(baskets, keys, weights=None):
    index = {key: i for i, key in enumerate(keys)}
    C = np.zeros((len(keys), len(keys)))
    for basket in baskets:
        for a, b in permutations(basket, 2):
            gain = 1 if weights is None else weights.get(a, 1) * weights.get(b, 1)
            C[index[a], index[b]] += gain / (len(basket) - 1)
    return C


def dense_ppmi(C):
    total = C.sum()
    row = C.sum(axis=1)
    P = C / total
    expected = np.outer(row, row) / total ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where((P > 0) & (expected > 0),
                         P / np.where(expected > 0, expected, 1.0), 1.0)
        return np.maximum(np.log(ratio), 0)


def dense_vectors(baskets, keys, dim, weights=None):
    ppmi = dense_ppmi(dense_cooc(baskets, keys, weights))
    omega = np.random.default_rng(0).standard_normal((len(keys), dim + 10))
    Q, _ = np.linalg.qr(ppmi @ omega)
    U, S, _ = np.linalg.svd(Q.T @ ppmi, full_matrices=False)
    V = (Q @ U)[:, :dim] * np.sqrt(S[:dim])
    return (V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)).astype(np.float32)


@pytest.mark.parametrize("weighted", [False, True])
@pytest.mark.parametrize("count", [9, 96])
def test_sparse_cooccurrence_ppmi_cosines_and_neighbors_match_dense(weighted, count):
    keys, baskets = fixture_baskets(count)
    # Cross the production accumulation boundary; overlap must accumulate correctly.
    baskets *= 2
    weights = {key: 1 + i % 5 / 3 for i, key in enumerate(keys)} if weighted else None
    dense = dense_cooc(baskets, keys, weights)
    sparse = training.cooc_matrix(baskets, keys, weights)
    assert issparse(sparse) and sparse.format == "csr"
    np.testing.assert_allclose(sparse.toarray(), dense, rtol=1e-13, atol=1e-13)
    np.testing.assert_allclose(training.positive_pmi(sparse).toarray(), dense_ppmi(dense),
                               rtol=1e-12, atol=1e-12)
    actual_keys, actual = training.svd(baskets, keys, 8, weights)
    expected = dense_vectors(baskets, keys, 8, weights)
    assert actual_keys == keys and actual.dtype == np.float32
    actual_cosine, expected_cosine = actual @ actual.T, expected @ expected.T
    np.testing.assert_allclose(actual_cosine, expected_cosine, atol=2e-6)
    np.testing.assert_array_equal(np.argsort(-actual_cosine, axis=1)[:, :6],
                                  np.argsort(-expected_cosine, axis=1)[:, :6])
    np.testing.assert_array_equal(training.svd(baskets, keys, 8, weights)[1], actual)


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("tol", [0, 1e-12])
def test_sparse_ppr_matches_dense_scores_rankings_and_mass(isolated, tol):
    keys, baskets = fixture_baskets()
    if isolated:
        keys += ["unconnected"]
        baskets += [["unconnected"]]
    C = dense_cooc(baskets, keys)
    col = C.sum(axis=0)
    dangling = col == 0
    col[dangling] = 1
    dense = C / col
    dense[:, dangling] = 1 / len(keys)
    store = SimpleNamespace(rec_evidence=SimpleNamespace(rec_baskets=lambda: baskets))
    actual_keys, transition, index = ppr.build_transition(store)
    assert actual_keys == sorted(keys)
    # The fixture's isolated key sorts last, so both representations have identical row order.
    seed = [index[keys[3]], index[keys[-1]]]
    expected = ppr.ppr_rank(dense, seed, iters=300, tol=tol)
    actual = ppr.ppr_rank(transition, seed, iters=300, tol=tol)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-14)
    np.testing.assert_array_equal(np.argsort(-actual), np.argsort(-expected))
    assert actual.sum() == pytest.approx(1)
    # Also exercise the operator's multi-column multiplication and isolated-column fallback.
    np.testing.assert_allclose(transition @ np.eye(len(keys)), dense, atol=1e-14)


def test_sparse_paths_do_not_allocate_or_convert_a_dense_square(monkeypatch):
    keys, baskets = fixture_baskets(256)
    n = len(keys)
    for name in ("zeros", "ones", "empty", "full"):
        original = getattr(np, name)

        def checked(shape, *args, _original=original, **kwargs):
            if isinstance(shape, tuple) and shape == (n, n):
                pytest.fail("Production allocated a dense vocabulary-sized square")
            return _original(shape, *args, **kwargs)

        monkeypatch.setattr(np, name, checked)

    def forbidden(*args, **kwargs):
        pytest.fail("Production densified sparse co-occurrence or formed a dense marginal product")

    monkeypatch.setattr(csr_array, "toarray", forbidden)
    monkeypatch.setattr(csr_array, "todense", forbidden)
    monkeypatch.setattr(np, "outer", forbidden)
    actual_keys, vectors = training.svd(baskets, keys, 8)
    assert actual_keys == keys and vectors.shape == (n, 8)
    store = SimpleNamespace(rec_evidence=SimpleNamespace(rec_baskets=lambda: baskets))
    _, transition, _ = ppr.build_transition(store)
    assert ppr.ppr_rank(transition, [0], tol=1e-8).sum() == pytest.approx(1)


def test_empty_singleton_duplicate_and_zero_weight_baskets():
    keys = ["a", "b", "isolated"]
    baskets = [[], ["isolated"], ["a", "a", "b"], ["b", "a"]]
    C = training.cooc_matrix(baskets, keys)
    np.testing.assert_array_equal(C.toarray(), [[0, 2, 0], [2, 0, 0], [0, 0, 0]])
    assert np.isfinite(training.positive_pmi(C).data).all()
    assert training.cooc_matrix([], []).shape == (0, 0)
    assert training.positive_pmi(csr_array((3, 3))).nnz == 0
    result_keys, vectors = training.svd(baskets, keys, 1, {"a": 0, "b": 0})
    assert result_keys == [] and vectors.shape == (0, 1)
    for basket_set in ([], [[], ["singleton"]]):
        result_keys, vectors = training.build_vectors_from_baskets(basket_set, dim=4)
        assert result_keys == [] and vectors.shape == (0, 4)


def test_small_library_still_builds_finite_vectors():
    keys = [str(i) for i in range(9)]
    result_keys, vectors = training.build_vectors_from_baskets([keys], dim=4)
    assert result_keys == keys and vectors.shape == (9, 4)
    assert np.isfinite(vectors).all()
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-6)


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 8, 9, 127, 128, 129, 999, 4097])
def test_bounded_shuffle_visits_every_index_once_and_is_seeded(count):
    def shuffled(seed):
        batches = list(_shuffled_indices(count, np.random.default_rng(seed), 17))
        assert all(0 < len(batch) <= 17 for batch in batches)
        return np.concatenate(batches) if batches else np.array([], dtype=np.int64)

    indices = shuffled(115)
    np.testing.assert_array_equal(np.sort(indices), np.arange(count))
    np.testing.assert_array_equal(indices, shuffled(115))
    if count > 8:
        assert not np.array_equal(indices, shuffled(116))


def test_pair_stream_preserves_every_pair_and_negative_sampling_counts():
    keys = list("abcdef")
    baskets = [list("abc"), list("bcd"), list("abc"), [], ["e"]]
    stream = PairStream(baskets, keys)
    expected = Counter(pair for basket in baskets for pair in permutations(basket, 2))
    pairs = [(keys[a], keys[b]) for ci, cj in stream.batches(np.random.default_rng(0), 5)
             for a, b in zip(ci, cj)]
    assert Counter(pairs) == expected and stream.count == len(pairs)
    assert all(a != b for a, b in pairs)
    np.testing.assert_array_equal(stream.context_counts,
        [sum(count for (a, b), count in expected.items() if b == key) for key in keys])


def test_huge_pair_universe_keeps_only_memberships_and_current_batch():
    keys = [str(i) for i in range(10000)]
    stream = PairStream([keys], keys)
    assert stream.count == 99990000
    assert sum(value.nbytes for value in vars(stream).values()
               if isinstance(value, np.ndarray)) < len(keys) * 32
    first = list(islice(stream.batches(np.random.default_rng(0), 64), 2))
    assert [len(ci) for ci, cj in first] == [64, 64]
    assert all(np.all(ci != cj) for ci, cj in first)


def test_streamed_item2vec_is_seeded_finite_and_preserves_output_format():
    keys, baskets = fixture_baskets(32)
    actual_keys, actual = training.item2vec(baskets, keys, 8, epochs=2)
    assert actual_keys == keys and actual.shape == (32, 8) and actual.dtype == np.float32
    assert np.isfinite(actual).all()
    np.testing.assert_allclose(np.linalg.norm(actual, axis=1), 1, atol=1e-6)
    np.testing.assert_array_equal(actual, training.item2vec(baskets, keys, 8, epochs=2)[1])


def test_streamed_item2vec_keeps_cluster_recall_against_former_full_shuffle(monkeypatch):
    rng = np.random.default_rng(115)
    groups = [[f"cluster-{j}-{i:02d}" for i in range(12)] for j in range(6)]
    keys = [key for group in groups for key in group]
    baskets = [rng.choice(group, size=int(rng.integers(5, 10)), replace=False).tolist()
               for group in groups for _ in range(40)]
    streamed = training.item2vec(baskets, keys, 16)[1]

    class FormerPairs:
        """The old materialized pair array and Fisher-Yates epoch order; same SGD below."""
        def __init__(self, baskets, keys):
            index = {key: i for i, key in enumerate(keys)}
            self.pairs = np.array([(index[a], index[b]) for basket in baskets
                                   for a, b in permutations(basket, 2)])
            self.count = len(self.pairs)
            self.context_counts = np.bincount(self.pairs[:, 1], minlength=len(keys))

        def batches(self, rng, batch_size):
            order = rng.permutation(self.count)
            for start in range(0, self.count, batch_size):
                batch = self.pairs[order[start:start + batch_size]]
                yield batch[:, 0], batch[:, 1]

    monkeypatch.setattr(training, "PairStream", FormerPairs)
    former = training.item2vec(baskets, keys, 16)[1]

    def cluster_recall(vectors):
        similarities = vectors @ vectors.T
        np.fill_diagonal(similarities, -np.inf)
        neighbors = np.argsort(-similarities, axis=1)[:, :5]
        return float(np.mean(neighbors // 12 == np.arange(len(keys))[:, None] // 12))

    before, after = cluster_recall(former), cluster_recall(streamed)
    assert after >= .8
    assert after >= before - .05


def test_cooccurrence_stops_between_sparse_products(monkeypatch):
    keys, baskets = fixture_baskets()
    baskets *= 3
    work = Activity()
    original = csc_array.__matmul__
    calls = []

    def multiply(self, other):
        result = original(self, other)
        calls.append(True)
        work.close_admission()
        return result

    monkeypatch.setattr(csc_array, "__matmul__", multiply)
    with pytest.raises(WorkClosing):
        work.wrap("embedding", training.cooc_matrix, baskets, keys)()
    assert len(calls) == 1 and work.wait(0)


def test_pair_shuffle_stops_before_generating_another_batch():
    work = Activity()

    def calculate():
        batches = _shuffled_indices(100000000, np.random.default_rng(0), 64)
        assert len(next(batches)) == 64
        work.close_admission()
        next(batches)

    with pytest.raises(WorkClosing):
        work.wrap("item2vec", calculate)()
    assert work.wait(0)
