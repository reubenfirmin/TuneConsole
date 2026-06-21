"""Offline evaluation of the recommender (spec §9/§10).

Ground truth is the user's own playlists: hold out one track from each playlist, rank all
library tracks by similarity to the centroid of the rest, and check whether the held-out
track lands in the top-k. recall@k is the share of playlists where it does — a single number
that says "does the model put tracks that genuinely belong together near each other?".
"""
import numpy as np

from yt_playlist import embed


def recall_at_k(store, k=20, min_size=5, seed=0) -> dict:
    """Leave-one-out recall@k over playlists, plus the random baseline for comparison."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"recall_at_k": None, "trials": 0, "reason": "no vectors built"}
    rng = np.random.default_rng(seed)
    hits = trials = 0
    n = len(keys)
    for p in store.get_playlists():
        members = [m for m in store.get_playlist_track_keys(p.id) if m in idx]
        if len(members) < min_size:
            continue
        held = members[int(rng.integers(len(members)))]
        rest = [m for m in members if m != held]
        c = V[[idx[m] for m in rest]].mean(0)
        c /= np.linalg.norm(c) + 1e-9
        sims = V @ c
        restset = set(rest)
        ranked = [keys[j] for j in np.argsort(-sims) if keys[j] not in restset]
        if ranked.index(held) < k:
            hits += 1
        trials += 1
    recall = hits / trials if trials else None
    baseline = k / n   # random pick from the library
    return {"recall_at_k": recall, "k": k, "trials": trials, "baseline": baseline,
            "lift": (recall / baseline) if recall and baseline else None}


def projection_recall(store, k=20) -> dict:
    """How well content predicts the embedding (the ACARec-flavored learned grounding's quality):
    hold out each tagged track, predict its vector from genre/year, and check whether the true track
    lands in the top-k by cosine. This is the 'groundability' of cold items — high means the learned
    projection is a viable cold-start grounding to compare against the bridge heuristic."""
    from yt_playlist.discover import ContentProjection
    from yt_playlist.rec_dao import RecDao
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"recall": None, "trials": 0}
    proj = ContentProjection.fit(store)
    if proj is None:
        return {"recall": None, "trials": 0}
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    hits = trials = 0
    for key, (g, y) in RecDao(store).track_content().items():
        if key not in idx:
            continue
        p = proj.predict(g, y)
        n = np.linalg.norm(p)
        if n == 0:
            continue
        sims = Vn @ (p / n)
        rank = int((sims > sims[idx[key]]).sum())   # how many tracks score above the true one
        hits += rank < k
        trials += 1
    return {"recall": hits / trials if trials else None, "k": k, "trials": trials}


def autotune(store, dims=(32, 48, 64), methods=("svd", "item2vec"), k=20) -> dict:
    """A/B the embedding *method* (svd vs item2vec) and dimensionality by recall@k, persist the
    winner, and rebuild on it. recall@k actually *tuning* the model (spec §10) — never forcing
    item2vec, only keeping it if it wins on your data. Returns the winner + per-config scores."""
    scores = {}
    for method in methods:
        store.set_setting("rec_embed_method", method)
        for d in dims:
            embed.build_and_store(store, dim=d)
            scores[(method, d)] = recall_at_k(store, k=k).get("recall_at_k") or 0.0
    best = max(scores, key=scores.get)
    bm, bd = best
    store.set_setting("rec_embed_method", bm)
    store.set_setting("rec_dim", str(bd))
    embed.build_and_store(store, dim=bd)     # leave the live model on the winning config
    return {"best_method": bm, "best_dim": bd,
            "scores": {f"{m}:{d}": v for (m, d), v in scores.items()}}
