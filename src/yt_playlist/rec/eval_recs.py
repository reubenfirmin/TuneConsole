"""Offline evaluation of the recommender (spec §9/§10).

Ground truth is the user's own playlists: hold out one track from each playlist, rank all
library tracks by similarity to the centroid of the rest, and check whether the held-out
track lands in the top-k. recall@k is the share of playlists where it does, a single number
that says "does the model put tracks that genuinely belong together near each other?".
"""
import numpy as np

from yt_playlist.rec import artist_model, embed
from yt_playlist.util import genre_map


def best_holdout(store, cap_days=30) -> int:
    """Adaptive holdout window for temporal_recall: clamp(round(span_days/3), 1, cap_days).

    A holdout bigger than a third of the span starves the context side of sufficient history
    to learn from, so we size the window proportionally to how much history exists."""
    lo, hi = store.history_bounds()
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
    for p in store.get_playlists():
        pa = sorted({artist_model._artist_of(m) for m in store.get_playlist_track_keys(p.id)} & set(idx))
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

    Using history_snapshots, hold out the most recent `holdout_days` of plays, treat everything played
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
    _, hi = store.history_bounds()
    if hi is None:
        return {"recall": None, "trials": 0, "reason": "no history"}
    cutoff = hi - holdout_days * 86400
    before = store.history_keys_before(cutoff)
    after = store.get_recent_history_keys(cutoff)
    context = [key for key in before if key in idx]
    held = [key for key in after if key in idx and key not in before]   # new plays to predict
    gen_only = store.generated_only_keys_since(cutoff)
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


def _era_band(y):
    return f"{int(y) // 10 * 10}s" if y else "unknown"


def _coverage_band(y, has_audio=False):
    """The content basis available for a (necessarily genre-tagged) track: which feature blocks it
    carries. §2 widened this with audio, so a track reads as genre / genre+year / genre+audio /
    genre+year+audio; it sharpens toward the richest band as enrichment fills audio coverage in."""
    parts = ["genre"]
    if y:
        parts.append("year")
    if has_audio:
        parts.append("audio")
    return "+".join(parts)


def projection_recall(store, k=20) -> dict:
    """How well content predicts the embedding (the ACARec-flavored learned grounding's quality):
    hold out each tagged track, predict its vector from genre/year, and check whether the true track
    lands in the top-k by cosine. This is the 'groundability' of cold items: high means the learned
    projection is a viable cold-start grounding to compare against the bridge heuristic.

    Also returns a `breakdown` partitioning trials by genre family, era, and coverage band, so a weak
    overall scalar can be traced to its failure modes (coarse genre vs low coverage vs feature basis)
    rather than read as one number (the §1b diagnosis lever)."""
    from yt_playlist.rec.discover import ContentProjection
    from yt_playlist.rec.rec_dao import RecDao
    keys, V, idx = embed.load_vectors(store)
    empty_bd = {"by_family": {}, "by_era": {}, "by_coverage": {}}
    if V is None:
        return {"recall": None, "trials": 0, "breakdown": empty_bd}
    proj = ContentProjection.fit(store)
    if proj is None:
        return {"recall": None, "trials": 0, "breakdown": empty_bd}
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    hits = trials = 0
    by_family, by_era, by_coverage = {}, {}, {}

    def _bump(d, key, hit):
        b = d.setdefault(key, {"hits": 0, "trials": 0})
        b["hits"] += int(hit)
        b["trials"] += 1

    dao = RecDao(store)
    content, audio = dao.track_content(), dao.track_audio_features()
    for key, (g, y) in content.items():
        if key not in idx:
            continue
        a = audio.get(key)
        p = proj.predict(g, y, a, artist=key.rsplit("|", 1)[-1])   # #28: fold in the artist signal
        n = np.linalg.norm(p)
        if n == 0:
            continue
        sims = Vn @ (p / n)
        rank = int((sims > sims[idx[key]]).sum())   # how many tracks score above the true one
        hit = rank < k
        hits += hit
        trials += 1
        _bump(by_family, genre_map.family(g), hit)
        _bump(by_era, _era_band(y), hit)
        _bump(by_coverage, _coverage_band(y, a is not None), hit)

    def _finalize(d):
        return {key: {"recall": b["hits"] / b["trials"] if b["trials"] else None, "trials": b["trials"]}
                for key, b in sorted(d.items(), key=lambda kv: -kv[1]["trials"])}

    return {"recall": hits / trials if trials else None, "k": k, "trials": trials,
            "breakdown": {"by_family": _finalize(by_family), "by_era": _finalize(by_era),
                          "by_coverage": _finalize(by_coverage)}}


def cold_rankable(store, now) -> dict:
    """#50 measured criterion: how many out-of-corpus (cold) tracks the cold ranker can actually score,
    vs the discovered-pool size. rankable > 0 means cold tracks are recommendable by taste + the
    transient tilts (not just recency/radio order). Imported lazily to dodge a surfaces<->eval cycle."""
    from yt_playlist.rec import surfaces
    pool = len(store.get_discovered_tracks())
    rankable = len(surfaces.cold_candidates(store, now, limit=10_000))
    return {"rankable": rankable, "pool": pool}


def content_rankable(store) -> dict:
    """#38 §3 measured: how many OWNED tracks that lack a co-occurrence vector become rankable through
    the content-space taste fit (a content vector present + content_taste built). Mirrors cold_rankable.
    Lazy imports avoid a scoring<->eval cycle."""
    from yt_playlist.rec.scoring import content_taste
    from yt_playlist.rec.rec_dao import RecDao
    _keys, _V, idx = embed.load_vectors(store)
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    owned = set(RecDao(store).library_keys())
    vectorless_owned = owned - set(idx)
    if CV is None or not content_taste(store):
        return {"rankable": 0, "vectorless_owned": len(vectorless_owned)}
    rankable = sum(1 for k in vectorless_owned if k in cidx)
    return {"rankable": rankable, "vectorless_owned": len(vectorless_owned)}


def _pick_metric(store, k) -> str:
    """Decide the sweep's metric ONCE, on the model as currently built (#83). autotune must never let a
    grid compare temporal scores against in-sample ones, so this is called a single time per run and the
    result is threaded through every score in that run rather than re-decided per call. Prefers
    temporal_recall, the model's real job (predict the next plays); falls back to recall@k only when
    history is too thin for a temporal split at pick time."""
    tr = temporal_recall(store, holdout_days=best_holdout(store), k=k).get("recall")
    return "temporal_recall" if tr is not None else "recall_at_k"


def _tune_score(store, k, metric):
    """Score the currently-built model for autotune (#38 §5, #83) with EXACTLY `metric`, never switching
    metric mid-sweep. If the temporal split goes unavailable for one config partway through a sweep (e.g.
    a dim change starves the context side), that config scores 0.0 and is flagged failed=True so the grid
    can show it and the winner can exclude it, rather than silently falling back to the in-sample metric.
    Returns (score, failed)."""
    if metric == "temporal_recall":
        tr = temporal_recall(store, holdout_days=best_holdout(store), k=k).get("recall")
        if tr is None:
            return 0.0, True
        return tr, False
    return recall_at_k(store, k=k).get("recall_at_k") or 0.0, False


def autotune(store, svd_dims=(48, 64, 96, 128), item2vec_probe_dim=64, k=20) -> dict:
    """A/B the embedding method+dimensionality, scored by the model's real job (temporal_recall, #38 §5)
    with a recall@k fallback when history is too thin for a temporal split, plus a single item2vec sanity
    probe. The metric is picked ONCE for the whole run (#83) so the grid's max() never compares temporal
    scores against in-sample ones. Persists and rebuilds on the winner. Returns the winner, the previous
    config, the full grid for the UI, and top-level `metric`/`in_sample` describing the run. Never forces
    item2vec; it's only kept if it wins on the user's data."""
    prev_method = store.get_setting("rec_embed_method") or "svd"
    prev_dim = int(store.get_setting("rec_dim") or embed.DIM)
    metric = _pick_metric(store, k)
    prev_score, prev_failed = _tune_score(store, k, metric)
    previous = {"method": prev_method, "dim": prev_dim, "recall": prev_score, "metric": metric}
    if prev_failed:
        previous["failed"] = True

    grid = []
    configs = [("svd", d) for d in svd_dims] + [("item2vec", item2vec_probe_dim)]
    for method, d in configs:
        store.set_setting("rec_embed_method", method)
        embed.build_and_store(store, dim=d)
        score, failed = _tune_score(store, k, metric)
        row = {"method": method, "dim": d, "recall": score, "metric": metric}
        if failed:
            row["failed"] = True
        grid.append(row)

    viable = [g for g in grid if not g.get("failed")]
    if not viable:
        # All configs failed. Restore the previous config instead of picking an arbitrary failed row.
        store.set_setting("rec_embed_method", prev_method)
        store.set_setting("rec_dim", str(prev_dim))
        embed.build_and_store(store, dim=prev_dim)
        result = {"winner": previous, "previous": previous, "grid": grid,
                  "metric": metric, "in_sample": metric == "recall_at_k", "sweep_failed": True}
    else:
        winner = max(viable, key=lambda g: g["recall"])
        store.set_setting("rec_embed_method", winner["method"])
        store.set_setting("rec_dim", str(winner["dim"]))
        embed.build_and_store(store, dim=winner["dim"])   # leave the live model on the winner
        result = {"winner": winner, "previous": previous, "grid": grid,
                  "metric": metric, "in_sample": metric == "recall_at_k"}
    return result


def cooc_weighting_ab(store, k=20) -> dict:
    """#38 §4c decision harness: does playcount-weighted co-occurrence beat binary membership? Build the
    embedding both ways, score each on temporal_recall (recall@k fallback), and report the winner. This
    is WHERE §4c is decided, on the real library's history, not in a unit test (which has none). Restores
    the prior `rec_cooc_weighting` setting and rebuilds the live model on it before returning.

    RESULT (2026-06-26, real library): weighting did NOT win, so the setting stays off. See the verdict
    note on embed._cooc_weights for the numbers.

    #83 fix carried here too: the metric is picked ONCE (on the model as currently built, before either
    arm is built) and held for both arms, so "binary" and "weighted" are always compared on the same
    metric. _tune_score also now sizes the temporal holdout via best_holdout(store) instead of a fixed
    30-day window, so a short retained history span (e.g. the real library's ~6 days) no longer forces a
    silent fallback to the weaker in-sample recall@k the way it used to. ALWAYS check the `metric` field:
    if it is "recall_at_k", the temporal split was unavailable even at the adaptive window, so re-judge by
    calling temporal_recall directly at a holdout_days that fits the span before trusting the verdict."""
    prev = store.get_setting("rec_cooc_weighting")
    metric = _pick_metric(store, k)
    out = {}
    for label, val in (("binary", "0"), ("weighted", "1")):
        store.set_setting("rec_cooc_weighting", val)
        embed.build_and_store(store)
        score, failed = _tune_score(store, k, metric)
        out[label] = {"score": score, "metric": metric}
        if failed:
            out[label]["failed"] = True
    store.set_setting("rec_cooc_weighting", prev if prev is not None else "0")
    embed.build_and_store(store)                       # restore the live model on the prior setting
    out["winner"] = "weighted" if out["weighted"]["score"] > out["binary"]["score"] else "binary"
    return out
