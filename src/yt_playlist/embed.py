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


def build_and_store(store, dim=None) -> int:
    """Build embeddings and persist them. Returns the number of tracks embedded.

    dim defaults to the recall@k-tuned `rec_dim` setting (or DIM if unset/untuned)."""
    if dim is None:
        dim = int(store.get_setting("rec_dim") or DIM)
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


def _kmeans(V, k, iters=25, seed=0):
    """Spherical k-means on L2-normalised vectors (cosine = dot). Returns a label per row."""
    rng = np.random.default_rng(seed)
    C = V[rng.choice(len(V), size=k, replace=False)].copy()
    labels = np.zeros(len(V), dtype=int)
    for _ in range(iters):
        labels = (V @ C.T).argmax(1)
        for j in range(k):
            members = V[labels == j]
            if len(members):
                c = members.mean(0)
                norm = np.linalg.norm(c)
                if norm > 0:
                    C[j] = c / norm
    return labels


def cluster(store, k=14):
    """Group the library's vectors into k coherent clusters: {cluster_id: [identity_key, ...]}."""
    keys, V, idx = load_vectors(store)
    if V is None or len(keys) < k:
        return {}
    labels = _kmeans(V, k)
    out: dict = {}
    for key, lab in zip(keys, labels):
        out.setdefault(int(lab), []).append(key)
    return out


def neighbors(store, key, topn=12, exclude=None):
    """Tracks most similar to one seed track in taste space."""
    keys, V, idx = load_vectors(store)
    if V is None or key not in idx:
        return []
    return _rank(keys, V, V[idx[key]], (exclude or set()) | {key}, topn)


def _centroid(V, idx, seed_groups):
    target = np.zeros(V.shape[1], dtype=np.float64)
    for ks, w in seed_groups:
        si = [idx[k] for k in ks if k in idx]
        if si:
            c = V[si].mean(0)
            n = np.linalg.norm(c)
            if n > 0:
                target += w * (c / n)
    n = np.linalg.norm(target)
    return target / n if n > 0 else None


def sims_for(store, seed_groups, keys):
    """Cosine similarity of each given key to the weighted taste centroid (for Tier-2 re-ranking).
    Returns {key: sim}; empty if no vectors. Spec §8 'refined by Tier-2'."""
    allkeys, V, idx = load_vectors(store)
    if V is None:
        return {}
    target = _centroid(V, idx, seed_groups)
    if target is None:
        return {}
    return {k: float(V[idx[k]] @ target) for k in keys if k in idx}


def blended_neighbors(store, seed_groups, topn=12, exclude=None):
    """Rank by a weighted blend of several seed centroids.

    seed_groups = [(keys, weight), ...] — e.g. slow all-time taste at 0.6 + fast recent-mood at
    0.4. Each group's centroid is normalised before weighting, so a small recent set still counts.
    """
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    target = np.zeros(V.shape[1], dtype=np.float64)
    excl = set(exclude or set())
    for ks, w in seed_groups:
        si = [idx[k] for k in ks if k in idx]
        excl |= set(ks)
        if si:
            c = V[si].mean(0)
            n = np.linalg.norm(c)
            if n > 0:
                target += w * (c / n)
    if not np.any(target):
        return []
    return _rank(keys, V, target / (np.linalg.norm(target) + 1e-9), excl, topn)


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
