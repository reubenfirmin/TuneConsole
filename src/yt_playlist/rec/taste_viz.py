"""Read-only transparency view of the recommendation model for the Taste page.

Pure functions over a Store (no web imports), like recommend.py. Assembles, per shared axis
(genre family / era decade / artist), the full ranking-multiplier stack -   effective = permanent_weight x standing_lean x transient_facet_multiplier (each clamped) - plus the graduation funnel (transient -> permanent) and the live transient sources. Nothing hidden:
this is the first place the transient model is ever surfaced (it otherwise only shapes ranking).
"""
import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, eval_recs, rec_params, recommend, transient
from yt_playlist.rec.rec_dao import RecDao


# Below this max-absolute share deviation (in share fraction, ~1 point) the right rose is treated as
# quiet - your recent mix matches your usual one, so there's nothing meaningful to draw.
QUIET_DEV_EPS = 0.01
# "Recent" = the last this-many play events (frequency-weighted). Large enough to be a representative
# recent mix, small enough to differ from your all-time mix for an active listener.
RECENT_PLAYS_WINDOW = 400
_ALLTIME_LIMIT = 1_000_000_000   # effectively unbounded - the all-time play-count basis


def _clamp(v):
    return max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, v))


def _axis_rows(prefix, shares, weights, standing, leans, fparams):
    rows = []
    for name, share in shares:
        token = f"{prefix}:{name}"
        pw = weights.get(token, 1.0)
        sl = standing.get(token, 1.0)
        tlean = leans.get(token, 0.0)
        tmult = transient.facet_multiplier(tlean, *fparams)
        rows.append({"name": name, "share": share, "permanent_weight": pw, "standing_lean": sl,
                     "transient_lean": tlean, "transient_mult": tmult,
                     "effective": _clamp(pw) * _clamp(sl) * _clamp(tmult)})
    return rows


def _attach_graduation(rows, prefix, theme, thr):
    for r in rows:
        score = theme.get(f"{prefix}:{r['name']}", 0.0)
        r["graduation"] = {"score": score, "threshold": thr,
                           "frac": max(-1.0, min(1.0, score / thr)) if thr else 0.0}
    return rows


def _axis_dist(store, counts, axis) -> dict:
    """Normalize a {identity_key: play count} map onto an axis (genre family / decade / artist),
    summing to 1. Play-frequency weighted - a track played 10× counts 10× - so recent and all-time
    are measured the same way and their difference is an honest 'more/less than usual'."""
    if not counts:
        return {}
    dao = RecDao(store)
    keys = list(counts)
    if axis == "genre":
        m, name_of = dao.track_genres(keys), genre_map.family
    elif axis == "era":
        m, name_of = dao.track_decades(keys), lambda d: d
    else:
        m, name_of = dao.track_artists(keys), lambda x: x
    w: dict = {}
    for k, c in counts.items():
        if k in m:
            name = name_of(m[k])
            if name not in (None, ""):
                w[name] = w.get(name, 0.0) + c
    total = sum(w.values())
    return {n: x / total for n, x in w.items()} if total else {}


def _attach_deviation(rows, recent, alltime):
    """Per axis row, attach `recent_share`, `alltime_share`, and `transient_dev` = recent - all-time
    (both play-frequency shares). The deviation is the zero-sum 'right now vs usual' signal the right
    rose draws. With no recent plays, deviation is 0 (not all-negative) so the rose is flat."""
    for r in rows:
        rs = recent.get(r["name"], 0.0)
        at = alltime.get(r["name"], 0.0)
        r["recent_share"] = rs
        r["alltime_share"] = at
        r["transient_dev"] = (rs - at) if recent else 0.0
    return rows


def _artist_shares(store, top=12):
    """Top artists by play share. Normalized over ALL artists (not just the displayed top-N), so the
    shares are directly comparable to the recency-weighted recent distribution - otherwise the recent
    side (full population) sits systematically below this one and every artist reads as 'less than
    usual'. Mirrors how the genre/era shares are full-population."""
    alla = store.top_artists(limit=1_000_000)          # all artists, play-desc (LIMIT only truncates output)
    total = sum(a.get("plays", 0) for a in alla)
    if not total:
        return []
    return [(a["artist"], a["plays"] / total) for a in alla[:top]]


def _sources(store):
    """#85: there is no longer a single rank-based recency alpha blending these sources - each one
    fades independently on its own wall-clock half-life (transient.decay_weight). Report those
    half-lives instead so the page states the real per-source decay, not a retired blend knob."""
    mood_pos = mood_neg = 0
    for _ts, direction, _keys in store.recent_mood_events():
        if direction > 0:
            mood_pos += 1
        elif direction < 0:
            mood_neg += 1
    limit = rec_params.get_param(store, "recent_play_limit")
    gp = rec_params.get_param
    return {
        "mood_pos": mood_pos, "mood_neg": mood_neg,
        "plays": len(store.recent_keys_ordered(0, limit=limit)),
        "likes": len(store.recent_liked_keys(limit=limit)),
        "dislikes": len(store.disliked_identity_keys()),
        "halflife_days": {
            "mood": gp(store, "mood_halflife_d"), "play": gp(store, "play_halflife_d"),
            "like": gp(store, "like_halflife_d"), "dislike": gp(store, "dislike_halflife_d"),
        },
    }


def model_transparency(store, now, recent_window=RECENT_PLAYS_WINDOW) -> dict:
    """The cheap transparency payload: per-axis layer stacks, lanes, breadth, freshness, sources, and
    the graduation funnel. Expensive panels (embedding/recall, playlist contexts, centroid tilt) are
    separate (engine_panel / centroid_tilt_panel), htmx-lazy on the page."""
    weights = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    standing = store.get_leans()
    leans = transient.facet_leans(store, now)
    theme = {r["facet"]: r["score"] for r in store.theme_rows()}
    fparams = (rec_params.get_param(store, "facet_gain"),
               rec_params.get_param(store, "facet_mult_min"),
               rec_params.get_param(store, "facet_mult_max"))
    graduation_threshold = rec_params.get_param(store, "theme_threshold")

    bd = recommend.taste_breadth(store)
    fam_shares = sorted(bd["families"].items(), key=lambda x: -x[1])
    genres = _attach_graduation(_axis_rows("genre", fam_shares, weights, standing, leans, fparams),
                                "genre", theme, graduation_threshold)
    eras = _attach_graduation(
        _axis_rows("era", recommend.era_distribution(store), weights, standing, leans, fparams),
        "era", theme, graduation_threshold)
    artists = _attach_graduation(
        _axis_rows("artist", _artist_shares(store), weights, standing, leans, fparams),
        "artist", theme, graduation_threshold)

    # The right rose / right bar plot 'right now vs your usual' as a zero-sum deviation: each facet's
    # share of your RECENT plays minus its share of all your plays - both play-frequency weighted, so
    # the difference is honest. Two count queries (recent window + all-time), mapped onto each axis.
    recent_counts = store.recent_play_counts(recent_window)
    alltime_counts = store.recent_play_counts(_ALLTIME_LIMIT)
    _attach_deviation(genres, _axis_dist(store, recent_counts, "genre"), _axis_dist(store, alltime_counts, "genre"))
    _attach_deviation(eras, _axis_dist(store, recent_counts, "era"), _axis_dist(store, alltime_counts, "era"))
    _attach_deviation(artists, _axis_dist(store, recent_counts, "artist"), _axis_dist(store, alltime_counts, "artist"))
    recent_exists = bool(recent_counts)

    sources = _sources(store)
    # Gates the roses' "Quiet right now" overlay: true iff you've played something recently AND your
    # recent mix actually differs from your usual one (otherwise every deviation is ~0 and the rose is
    # a flat ring - "same as usual", not a dramatic shape).
    max_dev = max((abs(r["transient_dev"]) for r in genres + eras + artists), default=0.0)
    has_transient = recent_exists and max_dev > QUIET_DEV_EPS

    return {
        "genres": genres, "eras": eras, "artists": artists,
        "lanes": [{"name": n, "label": lbl, "help": h, "weight": weights.get(f"lane:{n}", 1.0)}
                  for n, lbl, h in rec_params.LANES],
        "breadth": bd["breadth"], "n_families": bd["n_families"],
        # #85: no "freshness" key any more - the old sync-staleness relax of the whole transient read
        # is gone; each source in `sources` now fades independently on its own wall-clock half-life.
        "sources": sources,
        "funnel": [{"facet": f, "score": s, "threshold": graduation_threshold,
                    "frac": max(-1.0, min(1.0, s / graduation_threshold))}
                   for f, s in sorted(theme.items(), key=lambda x: -abs(x[1]))],
        "has_transient": has_transient,
        "recent_exists": recent_exists,
    }


def _dominant_family(store, pid) -> str:
    """The genre family a playlist leans on most - a concrete handle on its 'sound' for the viz."""
    from collections import Counter
    fams = Counter(genre_map.family(g) for g in store.playlist_track_genres(pid) if g)
    return fams.most_common(1)[0][0] if fams else ""


def engine_panel(store, top=12) -> dict:
    """The permanent embedding 'engine' - vectors/baskets/dim/method, recall@k, and the per-playlist
    taste contexts: which playlists the recommender blends to model your taste, each weighted by how
    much you listen to it, tagged with its dominant genre so the blend is legible."""
    contexts, total_contexts = [], 0
    pt = recommend.playlist_taste(store)
    if pt:
        order = list(np.argsort(-pt.weights))
        total_contexts = len(order)
        for i in order[:top]:
            pid = pt.pids[i] if i < len(pt.pids) else None
            contexts.append({"title": pt.titles[i], "weight": float(pt.weights[i]),
                             "genre": _dominant_family(store, pid) if pid is not None else ""})
    return {"vectors": store.rec_vectors_count(), "baskets": len(store.rec_baskets()),
            "dim": int(store.get_setting("rec_dim") or embed.DIM),
            "method": store.get_setting("rec_embed_method") or "auto",
            "recall": eval_recs.recall_at_k(store), "contexts": contexts,
            "contexts_total": total_contexts}


def centroid_tilt_panel(store, now) -> dict:
    """The transient embedding pull: magnitude of the current-mood centroid tilt, and its projection
    onto your top genre-family centroids - 'which way the mood leans'. Quiet -> 0.

    #85: `centroid_tilt` returns a unit direction (its wall-clock decay is baked in per-event before
    normalization), so magnitude is 1.0 whenever a tilt exists and 0.0 when quiet - there is no
    separate sync-staleness scale applied on top any more."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return {"magnitude": 0.0, "projection": []}
    tilt = transient.centroid_tilt(store, now, V, idx)
    if tilt is None:
        return {"magnitude": 0.0, "projection": []}
    mag = float(np.linalg.norm(tilt))
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
