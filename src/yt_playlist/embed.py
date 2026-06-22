"""Taste-embedding model: PPMI co-occurrence + truncated SVD over the user's own curation.

Builds a dense vector per track from how tracks co-occur across the user's playlists, albums,
and listening sessions — a latent model of *their* taste, not the crowd's. Neighbours in this
space capture second-order similarity (tracks that never share a playlist but both sit near a
third), which plain co-occurrence cannot. CPU-only, no GPU, no external models.
"""
import numpy as np

DIM = 48


def build_vectors(store, dim=DIM):
    """Return (keys, V), L2-normalised per-track embeddings. Method ('svd' default, or 'item2vec')
    comes from the recall@k-tuned `rec_embed_method` setting."""
    baskets = store.rec_baskets()
    keys = sorted({k for b in baskets for k in b})
    if len(keys) < dim + 5:
        return [], np.zeros((0, dim), dtype=np.float32)
    if (store.get_setting("rec_embed_method") or "svd") == "item2vec":
        return _item2vec(baskets, keys, dim)
    return _svd(baskets, keys, dim)


def _svd(baskets, keys, dim):
    """PPMI co-occurrence + randomised truncated SVD (numpy only)."""
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    C = np.zeros((n, n), dtype=np.float64)
    for b in baskets:                                  # Newman 1/(size-1) per pair
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
    rng = np.random.default_rng(0)
    om = rng.standard_normal((n, dim + 10))
    Q, _ = np.linalg.qr(ppmi @ om)
    Ub, S, _ = np.linalg.svd(Q.T @ ppmi, full_matrices=False)
    V = (Q @ Ub)[:, :dim] * np.sqrt(S[:dim])
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return keys, V.astype(np.float32)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _item2vec(baskets, keys, dim, epochs=5, neg=5, lr=0.025, seed=0):
    """item2vec: skip-gram with negative sampling over baskets-as-sentences (numpy SGNS).

    Each basket is an unordered 'sentence', so every co-member is a context. Slightly better than
    SVD on the long tail per the literature — gated by recall@k in autotune, never forced.
    """
    rng = np.random.default_rng(seed)
    n = len(keys)
    idx = {k: i for i, k in enumerate(keys)}
    pairs = []
    for b in baskets:
        ii = [idx[k] for k in b]
        for a in range(len(ii)):
            for c in range(len(ii)):
                if a != c:
                    pairs.append((ii[a], ii[c]))
    if not pairs:
        return keys, np.zeros((n, dim), dtype=np.float32)
    pairs = np.asarray(pairs, dtype=np.int64)
    counts = np.bincount(pairs[:, 1], minlength=n).astype(np.float64) + 1.0
    pdist = counts ** 0.75
    pdist /= pdist.sum()
    W = rng.standard_normal((n, dim)) * 0.01
    Cm = rng.standard_normal((n, dim)) * 0.01
    bs = 2048
    for _ in range(epochs):
        perm = rng.permutation(len(pairs))
        for s in range(0, len(pairs), bs):
            bp = pairs[perm[s:s + bs]]
            ci, cj = bp[:, 0], bp[:, 1]
            m = len(bp)
            negs = rng.choice(n, size=(m, neg), p=pdist)
            wi = W[ci]                                   # (m, dim)
            gp = (_sigmoid((wi * Cm[cj]).sum(1)) - 1.0)[:, None]          # positive grad
            gn = _sigmoid((wi[:, None, :] * Cm[negs]).sum(2))[:, :, None]  # (m, neg, 1)
            gW = gp * Cm[cj] + (gn * Cm[negs]).sum(1)
            np.add.at(W, ci, -lr * gW)
            np.add.at(Cm, cj, -lr * (gp * wi))
            np.add.at(Cm, negs.ravel(), (-lr * gn * wi[:, None, :]).reshape(-1, dim))
    V = W / (np.linalg.norm(W, axis=1, keepdims=True) + 1e-9)
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


def neighbors(store, key, topn=12, exclude=None):
    """Tracks most similar to one seed track in taste space."""
    keys, V, idx = load_vectors(store)
    if V is None or key not in idx:
        return []
    return _rank(keys, V, V[idx[key]], (exclude or set()) | {key}, topn)


def neighbors_for_unmodeled(store, key, topn=12):
    """Neighbours for a seed track that has no vector of its own — it's brand new, or quarantined out
    of the embedding (an unplayed generated track). Query with a proxy: the centroid of the seed
    artist's tracks that ARE modeled. Lets 'songs like this' work for such tracks without putting the
    track itself into the model. Empty if the artist has nothing modeled."""
    keys, V, idx = load_vectors(store)
    if V is None or key in idx:
        return []
    artist = key.rsplit("|", 1)[-1]                      # identity_key = "title|artist" (normalized)
    rows = [idx[k] for k in idx if k != key and k.rsplit("|", 1)[-1] == artist]
    if not rows:
        return []
    proxy = V[rows].mean(0)
    n = np.linalg.norm(proxy)
    return _rank(keys, V, proxy / n, {key}, topn) if n > 0 else []


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


CLUSTER_BETA = 0.6   # how hard a pruned ("negative model") track pushes a branch's ring away


def cluster_expand(store, pos_keys, neg_keys=(), exclude=None, topn=12, beta=CLUSTER_BETA):
    """A Clusters-canvas ring: tracks nearest a node's PINNED-path centroid, tilted AWAY from the
    PRUNED set's centroid. Score = cos(c, pos_centroid) - beta * cos(c, neg_centroid). pos/neg seeds
    and `exclude` (already-on-canvas keys) are never returned. Empty until the model is built.

    The candidate pool is today the user's own library vectors; a later pass can widen it to a
    new-music pool without touching this scoring."""
    keys, V, idx = load_vectors(store)
    if V is None:
        return []
    pi = [idx[k] for k in pos_keys if k in idx]
    if not pi:
        return []
    pos = V[pi].mean(0)
    pos /= np.linalg.norm(pos) + 1e-9
    scores = V @ pos
    ni = [idx[k] for k in neg_keys if k in idx]
    if ni:
        neg = V[ni].mean(0)
        neg /= np.linalg.norm(neg) + 1e-9
        scores = scores - beta * (V @ neg)
    excl = set(exclude or ()) | set(pos_keys) | set(neg_keys)
    out = []
    for j in np.argsort(-scores):
        k = keys[j]
        if k in excl:
            continue
        out.append((k, float(scores[j])))
        if len(out) >= topn:
            break
    return out


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
