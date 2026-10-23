"""Per-mode Personalized PageRank / random-walk-with-restart over the co-occurrence graph (#57).

Computed once per bundle rebuild with the configured solver. Home defaults to a live 50/50
PPR/cosine experiment. Final displayed cards and the other policy's counterfactual are recorded
in the local recommendation ledger. Only displayed cards receive observed outcomes.
"""
import numpy as np
from scipy.sparse.linalg import LinearOperator

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import embedding_training as training, embedding_vectors as vectors, rec_params, taste_modes

_ALPHA = 0.85          # walk weight; restart probability is (1 - alpha)
_ITERS = 50            # power-iteration steps (converges well before this on a stochastic matrix)


def build_transition(store):
    """(keys, W, idx): a column-stochastic co-occurrence transition matrix built from the SAME baskets
    the embedding uses. W[:, j] is the distribution of where a walker at track j steps next. Returns
    ([], None, {}) when there are no baskets."""
    baskets = store.rec_evidence.rec_baskets()
    keys = sorted({k for b in baskets for k in b})
    if not keys:
        return [], None, {}
    C = training.cooc_matrix(baskets, keys)          # symmetric Newman-weighted co-occurrence
    col = np.asarray(C.sum(axis=0)).ravel()
    dangling = col == 0                            # isolated track: no co-listen edges
    col[dangling] = 1.0
    W = C.multiply(1.0 / col).tocsr()
    if dangling.any():
        # Teleport from dangling nodes to the uniform distribution so the walk stays column-stochastic
        # and PPR mass is conserved (otherwise mass arriving at isolated tracks vanishes, biasing the
        # ranking against modes whose members have little co-listen data).
        # Apply the uniform term at multiplication time, rather than storing dense columns.
        def step(vector):
            return W @ vector + vector[dangling].sum(axis=0) / len(keys)

        transition = LinearOperator(W.shape, matvec=step, matmat=step, dtype=np.float64)
    else:
        transition = W
    return keys, transition, {k: i for i, k in enumerate(keys)}


def ppr_rank(W, seed_idx, alpha=_ALPHA, iters=_ITERS, tol=0.0):
    """Personalized PageRank scores via power iteration r = (1-alpha)*p + alpha*(W @ r), with the
    restart/personalization vector p uniform over seed_idx. Stops early once the L1 change drops
    below tol (tol=0.0 disables early-stop and runs the full iteration cap). Returns (n,)."""
    n = W.shape[0]
    p = np.zeros(n)
    if len(seed_idx) == 0:
        return p
    p[list(seed_idx)] = 1.0 / len(seed_idx)
    r = p.copy()
    for _ in range(iters):
        check_stopping()
        nxt = (1.0 - alpha) * p + alpha * (W @ r)
        if tol > 0.0 and np.abs(nxt - r).sum() < tol:
            return nxt
        r = nxt
    return r


def mode_rankings(store, alpha=None, iters=None, tol=None, depth=None, *, snapshot=None) -> dict:
    """{mode_id: [key, ...]} - per active taste mode, the co-listen PPR order of the graph's tracks,
    walk restarted uniformly over that mode's member tracks (library tracks nearest the mode centroid).
    Truncated to `depth`. {} when the graph or content space is missing; a mode with no member present
    in the co-listen vocab maps to []. Precompute only (called from the rec worker); None args fall back
    to the ppr_* knobs so a tuner can sweep alpha/tolerance without code changes. A bundle caller can
    supply its snapshot to keep these rankings in the same content space as its candidate buckets."""
    snapshot = snapshot if snapshot is not None else store.rec_model.content_generation()
    modes = taste_modes.live_modes(store, snapshot=snapshot)
    if not modes:
        return {}
    keys, W, idx = build_transition(store)
    lkeys, LV, lidx = vectors.load_content_vectors(store, snapshot=snapshot)
    if W is None or LV is None or not lkeys:
        return {}
    if alpha is None:
        alpha = float(rec_params.get_param(store, "ppr_alpha"))
    if iters is None:
        iters = int(rec_params.get_param(store, "ppr_iters"))
    if tol is None:
        tol = float(rec_params.get_param(store, "ppr_tol"))
    if depth is None:
        depth = int(rec_params.get_param(store, "ppr_rank_depth"))
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])
    if C.shape[1] != LV.shape[1]:
        # Stale modes: content space rebuilt at a new dim before the mode rebuild re-stacked the
        # centroids. {} (bundles then carry no _ppr and cards honestly fall back to cosine) beats
        # a shape-error crash in the rec worker.
        return {}
    near = (LV.astype(np.float64) @ C.T).argmax(axis=1)          # each library track -> nearest mode
    out = {}
    for j, m in enumerate(modes):
        check_stopping()
        members = [lkeys[i] for i in np.where(near == j)[0]]
        seed = [idx[k] for k in members if k in idx]             # members present in the co-listen vocab
        if not seed:
            out[m["mode_id"]] = []
            continue
        r = ppr_rank(W, seed, alpha=alpha, iters=iters, tol=tol)
        out[m["mode_id"]] = [keys[i] for i in np.argsort(-r)[:depth]]
    return out
