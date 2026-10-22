"""Offline evaluation of the recommender (spec §9/§10).

Ground truth is the user's own playlists: hold out one track from each playlist, rank all
library tracks by similarity to the centroid of the rest, and check whether the held-out
track lands in the top-k. recall@k is the share of playlists where it does, a single number
that says "does the model put tracks that genuinely belong together near each other?".
"""
import math

import numpy as np

from yt_playlist.rec import artist_model, embed
from yt_playlist.util import genre_map


def best_holdout(store, cap_days=30) -> int:
    """Adaptive holdout window for temporal_recall: clamp(round(span_days/3), 1, cap_days).

    A holdout bigger than a third of the span starves the context side of sufficient history
    to learn from, so we size the window proportionally to how much history exists."""
    lo, hi = store.rec_query.evaluation_history_bounds()
    if lo is None or hi is None:
        return 1
    span_s = hi - lo
    span_days = span_s / 86400.0
    if span_days == 0:
        return 1
    holdout = max(1, min(round(span_days / 3.0), cap_days))
    return holdout


def recall_at_k(store, k=20, min_size=5, seed=0) -> dict:
    """Leave-one-out recall@k over playlists, plus the random baseline for comparison."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"recall_at_k": None, "trials": 0, "reason": "no vectors built"}
    rng = np.random.default_rng(seed)
    hits = trials = 0
    n = len(keys)
    for p in store.playlists.get_playlists():
        members = [m for m in store.playlists.get_playlist_track_keys(p.id) if m in idx]
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


def artist_recall_at_k(store, k=10, min_size=3, seed=0) -> dict:
    """#28 Leave-one-out recall@k over playlists at the ARTIST level: hold out one artist from a
    playlist, rank all artists by collaborative-vector cosine to the centroid of the playlist's
    remaining artists, and check whether the held-out artist lands in the top-k. Validates §A (and,
    later, the blend weights); mirrors recall_at_k. recall=None when no artist vectors are built."""
    artists, V, idx = artist_model.load_artist_vectors(store)
    if V is None:
        return {"recall_at_k": None, "trials": 0, "reason": "no artist vectors built"}
    rng = np.random.default_rng(seed)
    hits = trials = 0
    n = len(artists)
    refs = store.artists.track_keys(library=True)
    for p in store.playlists.get_playlists():
        pa = sorted({a for m in store.playlists.get_playlist_track_keys(p.id) for a in refs.get(m,())} & set(idx))
        if len(pa) < min_size:
            continue
        held = pa[int(rng.integers(len(pa)))]
        rest = [a for a in pa if a != held]
        c = V[[idx[a] for a in rest]].mean(0)
        c /= np.linalg.norm(c) + 1e-9
        sims = V @ c
        restset = set(rest)
        ranked = [artists[j] for j in np.argsort(-sims) if artists[j] not in restset]
        if ranked.index(held) < k:
            hits += 1
        trials += 1
    recall = hits / trials if trials else None
    baseline = k / n if n else None
    return {"recall_at_k": recall, "k": k, "trials": trials, "baseline": baseline,
            "lift": (recall / baseline) if recall and baseline else None}


def temporal_recall(store, holdout_days=30, k=20) -> dict:
    """The model's actual job: predict what you'll play next, not reconstruct existing playlists.

    Using reconciled listening days, hold out the most recent `holdout_days` of plays, treat everything played
    before the cutoff as context, and check whether each genuinely new held-out play (one not already
    in the context) ranks in the top-k by cosine to the context centroid in the embedding space. This
    rewards forward prediction, unlike the in-sample leave-one-out recall_at_k.

    Held-out plays the app itself CAUSED are dropped first (#83's last circular path): a key whose
    every play_events row in the holdout window carries generated-playlist provenance
    (generated_only_keys_since) would otherwise count the model's own suggestion as a successful
    prediction. The number dropped is reported as `generated_excluded`. Returns recall=None when
    there are no vectors, no history, or the split has no usable context/held-out plays."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"recall": None, "trials": 0, "reason": "no vectors built"}
    _, hi = store.rec_query.evaluation_history_bounds()
    if hi is None:
        return {"recall": None, "trials": 0, "reason": "no history"}
    cutoff = hi - holdout_days * 86400
    before = store.history.history_keys_before(cutoff)
    after = store.history.get_recent_history_keys(cutoff)
    context = [key for key in before if key in idx]
    gen_only = store.rec_query.generated_only_keys_since(cutoff)
    # The daily projection already suppresses known sync shadows. Include the excluded
    # event keys for accounting, then remove them before calculating any score.
    held = [key for key in after | gen_only if key in idx and key not in before]
    generated_excluded = sum(1 for key in held if key in gen_only)
    held = [key for key in held if key not in gen_only]
    if not context or not held:
        return {"recall": None, "trials": len(held), "holdout_days": holdout_days,
                "generated_excluded": generated_excluded,
                "reason": "insufficient temporal split"}
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    c = Vn[[idx[key] for key in context]].mean(0)
    c /= np.linalg.norm(c) + 1e-9
    sims = Vn @ c
    ctxset = set(context)
    ranked = [keys[j] for j in np.argsort(-sims) if keys[j] not in ctxset]
    pos = {key: i for i, key in enumerate(ranked)}
    hits = sum(1 for h in held if pos.get(h, len(ranked)) < k)
    recall = hits / len(held)
    baseline = k / len(ranked) if ranked else None
    return {"recall": recall, "k": k, "trials": len(held), "holdout_days": holdout_days,
            "generated_excluded": generated_excluded,
            "baseline": baseline, "lift": (recall / baseline) if recall and baseline else None}


def _causal_window(store, cutoff, end, k, dim, method) -> dict:
    """Fit at ``cutoff`` and score organic, previously-unplayed outcomes in [cutoff, end).

    Unlike temporal_recall, this never loads the live vectors. The explicit as-of baskets are fitted
    in memory, so neither persisted model state nor a later basket can leak into the representation.
    """
    baskets, coverage = store.rec_query.rec_baskets_as_of(cutoff)
    weights = None
    if store.settings.get_setting("rec_cooc_weighting") == "1":
        weights = {key: 1.0 + math.log1p(count)
                   for key, count in store.history.play_counts_before(cutoff).items() if count}
    keys, V = embed.build_vectors_from_baskets(
        baskets, dim=dim, method=method, weights=weights)
    base = {"cutoff": cutoff, "end": end, "k": k, "coverage": coverage,
            "baskets": len(baskets), "vectors": len(keys)}
    if not keys:
        return {**base, "recall": None, "trials": 0, "generated_excluded": 0,
                "reason": "no reconstructable as-of vectors"}
    idx = {key: i for i, key in enumerate(keys)}
    before = store.history.history_keys_before(cutoff)
    after = store.history.history_keys_between(cutoff, end)
    context = [key for key in before if key in idx]
    gen_only = store.rec_query.generated_only_keys_between(cutoff, end)
    held = [key for key in after | gen_only if key in idx and key not in before]
    generated_excluded = sum(key in gen_only for key in held)
    held = [key for key in held if key not in gen_only]
    if not context or not held:
        return {**base, "recall": None, "trials": len(held),
                "generated_excluded": generated_excluded,
                "reason": "insufficient causal split"}
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    centroid = Vn[[idx[key] for key in context]].mean(0)
    centroid /= np.linalg.norm(centroid) + 1e-9
    sims = Vn @ centroid
    ranked = [keys[j] for j in np.argsort(-sims) if keys[j] not in before]
    pos = {key: i for i, key in enumerate(ranked)}
    hits = sum(pos.get(key, len(ranked)) < k for key in held)
    recall = hits / len(held)
    baseline = k / len(ranked) if ranked else None
    return {**base, "recall": recall, "trials": len(held), "hits": hits,
            "generated_excluded": generated_excluded, "baseline": baseline,
            "lift": (recall / baseline) if recall and baseline else None}


def rolling_temporal_recall(store, holdout_days=None, k=20, windows=3, dim=None, method=None) -> dict:
    """Leakage-free rolling forward-prediction evaluation.

    Each window independently fits an in-memory model from conservatively reconstructable state at
    its cutoff. Windows with insufficient evidence are retained in the result, never replaced by a
    different metric. The aggregate is trial-weighted and reports across-window variability.
    """
    if holdout_days is None:
        holdout_days = best_holdout(store)
    _lo, hi = store.rec_query.evaluation_history_bounds()
    if hi is None:
        return {"recall": None, "trials": 0, "windows": [], "viable_windows": 0,
                "holdout_days": holdout_days, "reason": "no history"}
    if dim is None:
        _keys, live, _idx = embed.load_vectors(store)
        dim = live.shape[1] if live is not None and live.ndim == 2 else embed.DIM
    if method is None:
        method = store.settings.get_setting("rec_embed_method") or "svd"
    span = holdout_days * 86400.0
    rows = []
    for i in range(max(1, int(windows))):
        end = hi - i * span + 1e-6       # include a snapshot exactly at the newest known timestamp
        cutoff = end - span
        rows.append(_causal_window(store, cutoff, end, k, int(dim), method))
    viable = [row for row in rows if row["recall"] is not None]
    trials = sum(row["trials"] for row in viable)
    hits = sum(row.get("hits", 0) for row in viable)
    recalls = [row["recall"] for row in viable]
    out = {"recall": hits / trials if trials else None, "k": k, "trials": trials,
           "windows": rows, "viable_windows": len(viable), "requested_windows": len(rows),
           "holdout_days": holdout_days, "method": method, "dim": int(dim),
           "generated_excluded": sum(row["generated_excluded"] for row in rows),
           "stddev": float(np.std(recalls)) if recalls else None,
           "causal": True, "partial_reconstruction": True}
    if not viable:
        out["reason"] = "no viable reconstructable windows"
    return out


def _era_band(y):
    return f"{int(y) // 10 * 10}s" if y else "unknown"


def _coverage_band(y, has_audio=False, has_genre=True):
    """The content basis available for a (necessarily genre-tagged) track: which feature blocks it
    carries. §2 widened this with audio, so a track reads as genre / genre+year / genre+audio /
    genre+year+audio; it sharpens toward the richest band as enrichment fills audio coverage in."""
    parts = ["genre"] if has_genre else []
    if y:
        parts.append("year")
    if has_audio:
        parts.append("audio")
    return "+".join(parts)


def projection_recall(store, k=20) -> dict:
    """Five-fold projection recall, grouped by track identity, against a fixed reference embedding.

    Every evaluated target is excluded from its fold's ridge targets, content vocabulary and audio
    statistics. Artists may cross folds; the precomputed artist feature block is disabled. This
    evaluates metadata projection generalization, not a fully held-out collaborative model or
    unseen-artist performance. Unscorable targets and insufficient training folds remain explicit.
    """
    from yt_playlist.rec.discover import ContentProjection
    keys, V, idx = embed.load_vectors(store)
    empty_bd = {"by_family": {}, "by_era": {}, "by_coverage": {}}
    result = {"recall": None, "trials": 0, "eligible": 0, "unscored": 0,
              "insufficient": 0, "k": k, "folds": 5, "split": "track_identity_5fold",
              "in_sample": False, "artist_features": False, "breakdown": empty_bd}
    if V is None:
        result["reason"] = "No reference embedding"
        return result
    dao = store.rec_query
    content, audio = dao.track_content(), dao.track_audio_features()
    eligible = sorted((set(content) | set(audio)) & set(idx))
    result["eligible"] = len(eligible)
    shuffled = np.random.default_rng(0).permutation(eligible).tolist()
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    hits = trials = 0
    by_family, by_era, by_coverage = {}, {}, {}

    def _bump(d, key, hit):
        b = d.setdefault(key, {"hits": 0, "trials": 0, "eligible": 0})
        b["eligible"] += 1
        if hit is not None:
            b["hits"] += int(hit)
            b["trials"] += 1

    for fold in range(5):
        held_out = shuffled[fold::5]
        if not held_out:
            continue
        training = set(idx) - set(held_out)
        proj = ContentProjection.fit(store, training_keys=training, include_artist=False)
        for key in held_out:
            g, y = content.get(key, (None, None))
            a = audio.get(key)
            hit = None
            if proj is None:
                result["insufficient"] += 1
            else:
                p = proj.predict(g, y, a)
                n = np.linalg.norm(p)
                if n > 0 and np.isfinite(n):
                    sims = Vn @ (p / n)
                    # Count ties conservatively: a feature-identical group is not perfect retrieval.
                    rank = int((sims >= sims[idx[key]] - 1e-9).sum())
                    hit = rank <= k
                    hits += int(hit)
                    trials += 1
                else:
                    result["unscored"] += 1
            _bump(by_family, genre_map.family(g) if g else "unknown", hit)
            _bump(by_era, _era_band(y), hit)
            _bump(by_coverage, _coverage_band(y, bool(a), bool(g)), hit)

    def _finalize(d):
        return {key: {"recall": b["hits"] / b["trials"] if b["trials"] else None,
                      "trials": b["trials"], "eligible": b["eligible"]}
                for key, b in sorted(d.items(), key=lambda kv: -kv[1]["trials"])}

    result.update(recall=hits / trials if trials else None, trials=trials,
                  breakdown={"by_family": _finalize(by_family), "by_era": _finalize(by_era),
                             "by_coverage": _finalize(by_coverage)})
    if not trials:
        result["reason"] = "Insufficient fitting evidence or no encodable held-out targets"
    return result


def cold_rankable(store, now) -> dict:
    """#50 measured criterion: how many out-of-corpus (cold) tracks the cold ranker can actually score,
    vs the discovered-pool size. rankable > 0 means cold tracks are recommendable by taste + the
    transient tilts (not just recency/radio order). Imported lazily to dodge a surfaces<->eval cycle."""
    from yt_playlist.rec import surfaces
    pool = len(store.discovery.get_discovered_tracks())
    candidates = surfaces.cold_candidates(store, now)
    rankable = sum(item.score is not None for item in candidates)
    return {"rankable": rankable, "fallback": len(candidates) - rankable,
            "eligible": len(candidates), "pool": pool}


def content_rankable(store) -> dict:
    """#38 §3 measured: how many OWNED tracks that lack a co-occurrence vector become rankable through
    the content-space taste fit (a content vector present + content_taste built). Mirrors cold_rankable.
    Lazy imports avoid a scoring<->eval cycle."""
    from yt_playlist.rec.scoring import content_taste
    _keys, _V, idx = embed.load_vectors(store)
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    owned = set(store.rec_query.library_keys())
    vectorless_owned = owned - set(idx)
    if CV is None or not content_taste(store):
        return {"rankable": 0, "vectorless_owned": len(vectorless_owned)}
    rankable = sum(1 for k in vectorless_owned if k in cidx)
    return {"rankable": rankable, "vectorless_owned": len(vectorless_owned)}


