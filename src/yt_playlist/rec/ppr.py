"""Per-mode Personalized PageRank / random-walk-with-restart over the co-occurrence graph (#57).

Run in SHADOW: computed each rebuild and logged alongside the centroid-cosine ranking for a later
NON-CIRCULAR comparison (does PPR surface better per-mode tracks than content-cosine?). It serves no
live surface and changes nothing the user sees - it only appends a snapshot to a persistent JSONL log
so we can revisit in a couple of weeks. Mode selection now rides on the #60C pick/impression signal
(#87); this shadow log will inform the eventual PPR verdict for live interleaving.
"""
import json

import numpy as np

from yt_playlist.core import paths
from yt_playlist.rec import embed, rec_params

_ALPHA = 0.85          # walk weight; restart probability is (1 - alpha)
_ITERS = 50            # power-iteration steps (converges well before this on a stochastic matrix)
_TOPN = 25             # how many tracks of each ranking to log per mode
_MAX_SNAPSHOTS = 400   # cap the shadow log: keep the most recent N rebuild snapshots
_LOG_NAME = "ppr_shadow_log.jsonl"


def build_transition(store):
    """(keys, W, idx): a column-stochastic co-occurrence transition matrix built from the SAME baskets
    the embedding uses. W[:, j] is the distribution of where a walker at track j steps next. Returns
    ([], None, {}) when there are no baskets."""
    baskets = store.rec_baskets()
    keys = sorted({k for b in baskets for k in b})
    if not keys:
        return [], None, {}
    C = embed._cooc_matrix(baskets, keys)          # symmetric Newman-weighted co-occurrence
    col = C.sum(axis=0)
    dangling = col == 0                            # isolated track: no co-listen edges
    col[dangling] = 1.0
    W = C / col
    if dangling.any():
        # Teleport from dangling nodes to the uniform distribution so the walk stays column-stochastic
        # and PPR mass is conserved (otherwise mass arriving at isolated tracks vanishes, biasing the
        # ranking against modes whose members have little co-listen data).
        W[:, dangling] = 1.0 / len(keys)
    return keys, W, {k: i for i, k in enumerate(keys)}


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
        nxt = (1.0 - alpha) * p + alpha * (W @ r)
        if tol > 0.0 and np.abs(nxt - r).sum() < tol:
            return nxt
        r = nxt
    return r


def mode_rankings(store, alpha=None, iters=None, tol=None, depth=None) -> dict:
    """{mode_id: [key, ...]} - per active taste mode, the co-listen PPR order of the graph's tracks,
    walk restarted uniformly over that mode's member tracks (library tracks nearest the mode centroid).
    Truncated to `depth`. {} when the graph or content space is missing; a mode with no member present
    in the co-listen vocab maps to []. Precompute only (called from the rec worker); None args fall back
    to the ppr_* knobs so a tuner can sweep alpha/tolerance without code changes."""
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return {}
    keys, W, idx = build_transition(store)
    lkeys, LV, lidx = embed.load_content_vectors(store)
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
        members = [lkeys[i] for i in np.where(near == j)[0]]
        seed = [idx[k] for k in members if k in idx]             # members present in the co-listen vocab
        if not seed:
            out[m["mode_id"]] = []
            continue
        r = ppr_rank(W, seed, alpha=alpha, iters=iters, tol=tol)
        out[m["mode_id"]] = [keys[i] for i in np.argsort(-r)[:depth]]
    return out


def shadow_log(store, now) -> int:
    """For each active taste mode, compute the PPR top-N (walk from the mode's members over the
    co-listen graph) and the centroid-cosine top-N (the mode's content-nearest members), and append one
    timestamped snapshot to the persistent shadow log. Returns the number of modes logged. Best-effort:
    returns 0 when prerequisites are missing; the caller guards against exceptions."""
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return 0
    keys, W, idx = build_transition(store)
    lkeys, LV, lidx = embed.load_content_vectors(store)
    if W is None or LV is None or not lkeys:
        return 0
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])
    if C.shape[1] != LV.shape[1]:
        return 0                    # stale modes (content space rebuilt at a new dim): skip quietly
    near = (LV.astype(np.float64) @ C.T).argmax(axis=1)            # each library track -> nearest mode
    meta = store.modes.meta_for(lkeys)

    def title(k):
        d = meta.get(k, {})
        return f"{(d.get('title') or '?')[:30]} - {(d.get('artist') or '?')[:18]}"

    snaps = []
    for j, m in enumerate(modes):
        members = [lkeys[i] for i in np.where(near == j)[0]]
        if not members:
            continue
        msims = LV[[lidx[k] for k in members]].astype(np.float64) @ C[j]
        cos_top = [members[i] for i in np.argsort(-msims)[:_TOPN]]
        seed = [idx[k] for k in members if k in idx]              # members present in the co-listen vocab
        if seed:
            r = ppr_rank(W, seed)
            ppr_top = [keys[i] for i in np.argsort(-r)[:_TOPN]]
        else:
            ppr_top = []
        snaps.append({"mode_id": m["mode_id"], "label": m["label"],
                      "members": len(members), "seed_in_graph": len(seed),
                      "cosine_top": [title(k) for k in cos_top],
                      "ppr_top": [title(k) for k in ppr_top]})
    rec = {"ts": float(now), "alpha": _ALPHA, "iters": _ITERS, "modes": snaps}
    log = paths.data_dir() / _LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    # Bounded append: keep only the most recent _MAX_SNAPSHOTS so the shadow log can't grow without end.
    prior = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    keep = prior[-(_MAX_SNAPSHOTS - 1):] if _MAX_SNAPSHOTS > 1 else []
    with open(log, "w", encoding="utf-8") as f:
        for line in keep:
            f.write(line + "\n")
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(snaps)
