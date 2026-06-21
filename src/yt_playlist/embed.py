"""Taste-embedding model: PPMI co-occurrence + truncated SVD over the user's own curation.

Builds a dense vector per track from how tracks co-occur across the user's playlists, albums,
and listening sessions — a latent model of *their* taste, not the crowd's. Neighbours in this
space capture second-order similarity (tracks that never share a playlist but both sit near a
third), which plain co-occurrence cannot. CPU-only, no GPU, no external models.
"""
import numpy as np

DIM = 48


def build_vectors(store, dim=DIM):
    """Return (keys, V) where V[i] is the L2-normalised embedding for keys[i]. May be empty."""
    baskets = store.rec_baskets()
    keys = sorted({k for b in baskets for k in b})
    n = len(keys)
    if n < dim + 5:
        return [], np.zeros((0, dim), dtype=np.float32)
    idx = {k: i for i, k in enumerate(keys)}

    # weighted co-occurrence: each basket contributes 1/(size-1) per pair (Newman weighting),
    # so a 100-track playlist doesn't drown out a tight 8-track one.
    C = np.zeros((n, n), dtype=np.float64)
    for b in baskets:
        ii = [idx[k] for k in b]
        w = 1.0 / (len(ii) - 1)
        for a in range(len(ii)):
            ia = ii[a]
            for c in range(a + 1, len(ii)):
                ic = ii[c]
                C[ia, ic] += w
                C[ic, ia] += w

    tot = C.sum()
    if tot <= 0:
        return [], np.zeros((0, dim), dtype=np.float32)
    row = C.sum(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        P = C / tot
        exp = np.outer(row, row) / (tot * tot)
        ppmi = np.maximum(np.log(np.where((P > 0) & (exp > 0), P / np.where(exp > 0, exp, 1.0), 1.0)), 0.0)

    # randomised truncated SVD (numpy only): Q captures the top subspace, then SVD the small B.
    rng = np.random.default_rng(0)
    om = rng.standard_normal((n, dim + 10))
    Q, _ = np.linalg.qr(ppmi @ om)
    Ub, S, _ = np.linalg.svd(Q.T @ ppmi, full_matrices=False)
    V = (Q @ Ub)[:, :dim] * np.sqrt(S[:dim])
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return keys, V.astype(np.float32)


def build_and_store(store, dim=DIM) -> int:
    """Build embeddings and persist them. Returns the number of tracks embedded."""
    keys, V = build_vectors(store, dim)
    store.replace_rec_vectors([(k, V[i].tobytes()) for i, k in enumerate(keys)])
    return len(keys)


def load_vectors(store):
    """Return (keys, V, idx) from persisted vectors, or ([], None, {}) if none built yet."""
    rows = store.get_rec_vectors()
    if not rows:
        return [], None, {}
    keys = [k for k, _ in rows]
    V = np.stack([np.frombuffer(v, dtype=np.float32) for _, v in rows])
    return keys, V, {k: i for i, k in enumerate(keys)}


def _rank(keys, V, target, exclude, topn):
    sims = V @ target
    out = []
    for j in np.argsort(-sims):
        k = keys[j]
        if k in exclude:
            continue
        out.append((k, float(sims[j])))
        if len(out) >= topn:
            break
    return out


def neighbors(store, key, topn=12, exclude=None):
    """Tracks most similar to one seed track in taste space."""
    keys, V, idx = load_vectors(store)
    if V is None or key not in idx:
        return []
    return _rank(keys, V, V[idx[key]], (exclude or set()) | {key}, topn)


def centroid_neighbors(store, seed_keys, topn=12, exclude=None):
    """Tracks most similar to the centroid of a set of seeds (e.g. a playlist's vibe)."""
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    si = [idx[k] for k in seed_keys if k in idx]
    if not si:
        return []
    centroid = V[si].mean(0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    return _rank(keys, V, centroid, set(exclude or set()) | set(seed_keys), topn)
