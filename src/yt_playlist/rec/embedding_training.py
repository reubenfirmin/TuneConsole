"""Collaborative fitting from detached curation baskets and optional per-key gains.

No repository or publication access: backtests and maintenance fit the same sparse
algorithms without touching the currently served model.
"""
import math
from typing import NamedTuple

import numpy as np
from scipy.sparse import csr_array

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec.pair_stream import PairStream
from yt_playlist.rec.vector_math import NORM_EPS

DIM = 48
MIN_VOCAB_MARGIN = 5
_SVD_OVERSAMPLE = 10
_NEG_SAMPLE_SMOOTH = 0.75
_INIT_SCALE = 0.01
_SGNS_BATCH = 2048
_COOC_BASKET_BATCH = 256


class FittedVectors(NamedTuple):
    keys: list[str]
    matrix: np.ndarray


def build_vectors_from_baskets(baskets, dim=DIM, method="svd", weights=None) -> FittedVectors:
    check_stopping()
    keys = sorted({k for b in baskets for k in b})
    if len(keys) < dim + MIN_VOCAB_MARGIN:
        return FittedVectors([], np.zeros((0, dim), dtype=np.float32))
    if method == "item2vec":
        return item2vec(baskets, keys, dim)
    return svd(baskets, keys, dim, weights)


def cooc_weights(plays):
    """Optional gain 1 + log1p(listening-day count); never-played keys retain 1."""
    return {k: 1.0 + math.log1p(c) for k, c in plays.items() if c}


def cooc_matrix(baskets, keys, weights=None):
    """Sparse symmetric Newman co-occurrence; bounded basket incidence batches."""
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    g = None if weights is None else np.array([float(weights.get(k, 1.0)) for k in keys])
    C = csr_array((n, n), dtype=np.float64)
    for start in range(0, len(baskets), _COOC_BASKET_BATCH):
        check_stopping()
        indices, indptr, gains = [], [0], []
        for b in baskets[start:start + _COOC_BASKET_BATCH]:
            check_stopping()
            ii = [idx[k] for k in dict.fromkeys(b)]
            if len(ii) < 2:
                continue
            indices.extend(ii)
            indptr.append(len(indices))
            gains.append(1.0 / (len(ii) - 1))
        if not gains:
            continue
        incidence = csr_array((np.ones(len(indices)), np.asarray(indices, dtype=np.int64),
                               np.asarray(indptr, dtype=np.int64)), shape=(len(gains), n))
        block = (incidence.T @ incidence.multiply(np.asarray(gains)[:, None])).tocsr()
        block.setdiag(0)
        block.eliminate_zeros()
        C = C + block
    check_stopping()
    if g is not None:
        C = C.multiply(g[:, None]).multiply(g[None, :]).tocsr()
        C.eliminate_zeros()
    return C


def positive_pmi(C):
    """PPMI has no absent co-occurrences; never form a dense marginal product."""
    total = float(C.sum())
    if total <= 0:
        return csr_array(C.shape, dtype=np.float64)
    row = np.asarray(C.sum(axis=1)).ravel()
    entries = C.tocoo(copy=False)
    values = np.maximum(np.log((entries.data / row[entries.row])
                               * (total / row[entries.col])), 0.0)
    ppmi = csr_array((values, (entries.row, entries.col)), shape=C.shape)
    ppmi.eliminate_zeros()
    return ppmi


def svd(baskets, keys, dim, weights=None) -> FittedVectors:
    """Sparse PPMI and deterministic truncated SVD with only n-by-sketch dense arrays."""
    n = len(keys)
    ppmi = positive_pmi(cooc_matrix(baskets, keys, weights))
    if not ppmi.nnz:
        return FittedVectors([], np.zeros((0, dim), dtype=np.float32))
    rng = np.random.default_rng(0)
    om = rng.standard_normal((n, dim + _SVD_OVERSAMPLE))
    check_stopping()
    Q, _ = np.linalg.qr(ppmi @ om)
    check_stopping()
    Ub, S, _ = np.linalg.svd((ppmi.T @ Q).T, full_matrices=False)
    check_stopping()
    V = (Q @ Ub)[:, :dim] * np.sqrt(S[:dim])
    V /= np.linalg.norm(V, axis=1, keepdims=True) + NORM_EPS
    return FittedVectors(keys, V.astype(np.float32))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def item2vec(baskets, keys, dim, epochs=5, neg=5, lr=0.025, seed=0) -> FittedVectors:
    """Deterministic skip-gram negative sampling over the bounded unordered pair stream."""
    rng = np.random.default_rng(seed)
    n = len(keys)
    pairs = PairStream(baskets, keys)
    if not pairs.count:
        return FittedVectors(keys, np.zeros((n, dim), dtype=np.float32))
    pdist = (pairs.context_counts + 1.0) ** _NEG_SAMPLE_SMOOTH
    pdist /= pdist.sum()
    W = rng.standard_normal((n, dim)) * _INIT_SCALE
    Cm = rng.standard_normal((n, dim)) * _INIT_SCALE
    bs = _SGNS_BATCH
    for _ in range(epochs):
        for ci, cj in pairs.batches(rng, bs):
            check_stopping()
            m = len(ci)
            negs = rng.choice(n, size=(m, neg), p=pdist)
            wi = W[ci]
            gp = (_sigmoid((wi * Cm[cj]).sum(1)) - 1.0)[:, None]
            gn = _sigmoid((wi[:, None, :] * Cm[negs]).sum(2))[:, :, None]
            gW = gp * Cm[cj] + (gn * Cm[negs]).sum(1)
            np.add.at(W, ci, -lr * gW)
            np.add.at(Cm, cj, -lr * (gp * wi))
            np.add.at(Cm, negs.ravel(), (-lr * gn * wi[:, None, :]).reshape(-1, dim))
    V = W / (np.linalg.norm(W, axis=1, keepdims=True) + NORM_EPS)
    return FittedVectors(keys, V.astype(np.float32))
