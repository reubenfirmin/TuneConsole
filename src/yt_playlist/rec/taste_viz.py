"""Read-only transparency view of the recommendation model for the Taste page.

Pure functions over a Store (no web imports), like recommend.py. Assembles, per shared axis
(genre family / era decade / artist), the full ranking-multiplier stack —
  effective = permanent_weight x standing_lean x transient_facet_multiplier (each clamped) —
plus the graduation funnel (transient -> permanent) and the live transient sources. Nothing hidden:
this is the first place the transient model is ever surfaced (it otherwise only shapes ranking).
"""
import numpy as np

from yt_playlist.rec import embed, eval_recs, genre_map, rec_params, recommend, transient
from yt_playlist.rec.rec_dao import RecDao


# Below this max-absolute lean the transient model is treated as quiet: the signed roses normalize to
# max-abs, so a near-dead mood would otherwise still render a full-amplitude (misleading) shape.
QUIET_LEAN_EPS = 0.02


def _clamp(v):
    return max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, v))


def _axis_rows(prefix, shares, weights, standing, leans):
    rows = []
    for name, share in shares:
        token = f"{prefix}:{name}"
        pw = weights.get(token, 1.0)
        sl = standing.get(token, 1.0)
        tlean = leans.get(token, 0.0)
        tmult = transient.facet_multiplier(tlean)
        rows.append({"name": name, "share": share, "permanent_weight": pw, "standing_lean": sl,
                     "transient_lean": tlean, "transient_mult": tmult,
                     "effective": _clamp(pw) * _clamp(sl) * _clamp(tmult)})
    return rows


def _attach_graduation(rows, prefix, theme):
    thr = rec_params.THEME_THRESHOLD
    for r in rows:
        score = theme.get(f"{prefix}:{r['name']}", 0.0)
        r["graduation"] = {"score": score, "threshold": thr,
                           "frac": max(-1.0, min(1.0, score / thr)) if thr else 0.0}
    return rows


def _artist_shares(store, top=12):
    """Top artists by play share (mirrors the play-weighted genre/era shares)."""
    counts = {a["artist"]: a.get("total", 0) for a in store.top_artists(top)}
    total = sum(counts.values())
    if not total:
        return []
    return sorted(((a, c / total) for a, c in counts.items()), key=lambda x: -x[1])


def _sources(store):
    mood_pos = mood_neg = 0
    for _ts, direction, _keys in store.recent_mood_events():
        if direction > 0:
            mood_pos += 1
        elif direction < 0:
            mood_neg += 1
    return {
        "mood_pos": mood_pos, "mood_neg": mood_neg,
        "plays": len(store.recent_keys_ordered(0, limit=rec_params.RECENT_PLAY_LIMIT)),
        "likes": len(store.recent_liked_keys(limit=rec_params.RECENT_PLAY_LIMIT)),
        "dislikes": len(store.disliked_identity_keys()),
        "alpha": rec_params.MOOD_RECENCY_ALPHA,
    }


def model_transparency(store, now) -> dict:
    """The cheap transparency payload: per-axis layer stacks, lanes, breadth, freshness, sources, and
    the graduation funnel. Expensive panels (embedding/recall, playlist contexts, centroid tilt) are
    separate (engine_panel / centroid_tilt_panel), htmx-lazy on the page."""
    weights = store.get_weights()
    standing = store.get_leans()
    leans = transient.facet_leans(store, now)
    theme = {r["facet"]: r["score"] for r in store.theme_rows()}

    bd = recommend.taste_breadth(store)
    fam_shares = sorted(bd["families"].items(), key=lambda x: -x[1])
    genres = _attach_graduation(_axis_rows("genre", fam_shares, weights, standing, leans), "genre", theme)
    eras = _attach_graduation(
        _axis_rows("era", recommend.era_distribution(store), weights, standing, leans), "era", theme)
    artists = _attach_graduation(
        _axis_rows("artist", _artist_shares(store), weights, standing, leans), "artist", theme)

    factor = transient.staleness_factor(store, now)
    sources = _sources(store)
    # Gates the roses' "Quiet right now" overlay: true iff the transient leans the roses draw are
    # meaningfully nonzero. A very stale sync decays every lean toward ~0; below QUIET_LEAN_EPS we
    # honestly say it's quiet rather than normalize a dead mood into a dramatic shape. Source counts
    # render in their own panel either way.
    max_lean = max((abs(r["transient_lean"]) for r in genres + eras + artists), default=0.0)
    has_transient = max_lean > QUIET_LEAN_EPS

    return {
        "genres": genres, "eras": eras, "artists": artists,
        "lanes": [{"name": n, "label": lbl, "weight": weights.get(f"lane:{n}", 1.0)}
                  for n, lbl, _ in rec_params.LANES],
        "breadth": bd["breadth"],
        "freshness": {"factor": factor, "halflife_days": rec_params.STALE_DECAY_HALFLIFE_D,
                      "live": factor >= 0.999},
        "sources": sources,
        "funnel": [{"facet": f, "score": s, "threshold": rec_params.THEME_THRESHOLD,
                    "frac": max(-1.0, min(1.0, s / rec_params.THEME_THRESHOLD))}
                   for f, s in sorted(theme.items(), key=lambda x: -abs(x[1]))],
        "has_transient": has_transient,
    }


def engine_panel(store) -> dict:
    """The permanent embedding 'engine' — vectors/baskets/dim/method, recall@k, and the per-playlist
    taste contexts (which playlists shape taste, weighted by how much you play them)."""
    contexts = []
    pt = recommend.playlist_taste(store)
    if pt:
        order = np.argsort(-pt.weights)
        contexts = [{"title": pt.titles[i], "weight": float(pt.weights[i])} for i in order]
    return {"vectors": store.rec_vectors_count(), "baskets": len(store.rec_baskets()),
            "dim": int(store.get_setting("rec_dim") or embed.DIM),
            "method": store.get_setting("rec_embed_method") or "auto",
            "recall": eval_recs.recall_at_k(store), "contexts": contexts}


def centroid_tilt_panel(store, now) -> dict:
    """The transient embedding pull: magnitude of the current-mood centroid tilt (staleness-scaled),
    and its projection onto your top genre-family centroids — 'which way the mood leans'. Quiet -> 0."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"magnitude": 0.0, "projection": []}
    tilt = transient.centroid_tilt(store, now, V, idx)
    if tilt is None:
        return {"magnitude": 0.0, "projection": []}
    mag = float(np.linalg.norm(tilt)) * transient.staleness_factor(store, now)
    tn = tilt / (np.linalg.norm(tilt) + 1e-9)
    fam_keys: dict = {}
    tg = RecDao(store).track_genres(list(keys))
    for k in keys:
        if k in tg:
            fam_keys.setdefault(genre_map.family(tg[k]), []).append(k)
    proj = []
    for fam, ks in fam_keys.items():
        rows = [idx[k] for k in ks if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        c = c / (np.linalg.norm(c) + 1e-9)
        proj.append({"name": fam, "value": float(c @ tn)})
    proj.sort(key=lambda x: -abs(x["value"]))
    return {"magnitude": mag, "projection": proj[:6]}
