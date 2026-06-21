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


def autotune(store, dims=(32, 48, 64), k=20) -> dict:
    """Pick the embedding dimensionality that maximizes recall@k, persist it, and rebuild on it.

    This is recall@k actually *tuning* the model (spec §10), not just reporting it. Returns the
    winning dim and the per-dim scores."""
    scores = {}
    for d in dims:
        embed.build_and_store(store, dim=d)
        scores[d] = recall_at_k(store, k=k).get("recall_at_k") or 0.0
    best = max(scores, key=scores.get)
    store.set_setting("rec_dim", str(best))
    embed.build_and_store(store, dim=best)   # leave the live model on the winning dim
    return {"best_dim": best, "scores": scores}
