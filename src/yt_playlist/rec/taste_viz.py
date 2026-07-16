"""Read-only transparency view of the recommendation model for the Taste page.

Pure functions over a Store (no web imports), like recommend.py.

#88: the model is a stack of four layers, each at its own wall-clock timescale:

    NOW        hours       a posterior over your taste modes (layers.now_mode_mix)
    SESSION    a day       the same posterior, decay-weighted (layers.session_mode_mix),
                           plus a free direction in the embedding (layers.session_tilt)
    TRANSIENT  days-weeks  per-facet leans -> ranking multipliers (transient.facet_leans),
                           plus a free direction in the embedding (transient.centroid_tilt)
    PERMANENT  forever     graduated weights x standing leans, and the embedding itself

A layer only exists on the axes where the model actually computes it. NOW and SESSION are
categorical over taste MODES and have no per-facet quantity at all; NOW deliberately has no
embedding direction (averaging an hour of plays yields a direction nothing in the catalogue sounds
like - see layers.py). So this module exposes each layer on the axis it lives on, and nowhere else:

    mode_layers()          modes axis:    NOW, SESSION, PERMANENT
    model_transparency()   facet axes:    TRANSIENT, PERMANENT (and their product)
    centroid_tilt_panel()  embedding:     SESSION, TRANSIENT

Nothing hidden: this is the only place the transient model is ever surfaced (it otherwise only
shapes ranking).
"""
import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, eval_recs, layers, rec_params, recommend, transient
from yt_playlist.rec.rec_dao import RecDao


def _axis_rows(prefix, shares, weights, standing, leans, fparams):
    """One row per facet on an axis, carrying the EXACT multiplier chain `scoring._axis_mult` applies:

        effective = permanent_weight x standing_lean x transient_mult

    No clamping is re-applied here. Each factor is already bounded where it is produced (genre
    weights at set_weight, the facet multiplier inside transient.facet_multiplier), so re-clamping
    would show the user a number the ranker never used.
    """
    rows = []
    for name, share in shares:
        token = f"{prefix}:{name}"
        pw = weights.get(token, 1.0)
        sl = standing.get(token, 1.0)
        tlean = leans.get(token, 0.0)
        tmult = transient.facet_multiplier(tlean, *fparams)
        rows.append({"name": name, "share": share,
                     "permanent_weight": pw, "standing_lean": sl, "lasting": pw * sl,
                     "transient_lean": tlean, "transient_mult": tmult,
                     "effective": pw * sl * tmult})
    return rows


def _attach_graduation(rows, prefix, theme, thr):
    for r in rows:
        score = theme.get(f"{prefix}:{r['name']}", 0.0)
        r["graduation"] = {"score": score, "threshold": thr,
                           "frac": max(-1.0, min(1.0, score / thr)) if thr else 0.0}
    return rows


def _breadth_word(breadth):
    """Same thresholds the 'Taste breadth' card uses: >0.66 eclectic, <0.33 focused, else balanced."""
    return "eclectic" if breadth > 0.66 else ("focused" if breadth < 0.33 else "balanced")


def _artist_shares(store, top=12):
    """Top artists by play share, normalized over ALL artists (not just the displayed top-N), so the
    shares are comparable to the genre/era shares, which are likewise full-population."""
    alla = store.top_artists(limit=1_000_000)          # all artists, play-desc (LIMIT only truncates output)
    total = sum(a.get("plays", 0) for a in alla)
    if not total:
        return []
    return [(a["artist"], a["plays"] / total) for a in alla[:top]]


def _ribbon_segments(shares, modes):
    """{mode_id: share} + the active-mode list -> ribbon segment dicts, each carrying `color_idx`
    (its position in `modes`, NOT in the ribbon) so the same taste mode is the same color in the
    NOW, SESSION and PERMANENT ribbons even when a mode is missing from one of them."""
    if not shares:
        return []
    return [{"mode_id": m["mode_id"], "label": m["label"], "share": shares[m["mode_id"]], "color_idx": i}
            for i, m in enumerate(modes) if m["mode_id"] in shares]


def _permanent_mode_shares(modes):
    """Each active taste mode's share of the clustered library, by mode size. This is the PERMANENT
    reading on the modes axis: the durable shape of your taste in mode space, the thing NOW and
    SESSION are transient departures from."""
    total = sum(m["size"] for m in modes)
    return {m["mode_id"]: m["size"] / total for m in modes if m["size"]} if total else {}


def mode_layers(store, now) -> dict:
    """The modes axis, read at three timescales - the only axis on which NOW and SESSION exist.

    All three readings are shares over the SAME active-mode list, so they stack: PERMANENT is the
    durable shape of your taste, SESSION is this sitting's balance, NOW is the last few hours. NOW
    and SESSION are confidence-gated (`now_min_events` distinct played keys with a known sound);
    below the gate they report nothing rather than a weak guess, and `segments` is empty.
    """
    now_shares, now_n, modes = layers.now_mode_mix(store, now)
    session_shares, session_n, _ = layers.session_mode_mix(store, now)
    return {
        "modes": modes,
        "now": {"segments": _ribbon_segments(now_shares, modes), "n": now_n,
                "window_h": rec_params.get_param(store, "now_window_h")},
        "session": {"segments": _ribbon_segments(session_shares, modes), "n": session_n,
                    "halflife_h": rec_params.get_param(store, "session_halflife_h")},
        "permanent": {"segments": _ribbon_segments(_permanent_mode_shares(modes), modes),
                      "n": sum(m["size"] for m in modes)},
        "min_events": int(rec_params.get_param(store, "now_min_events")),
    }


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


def model_transparency(store, now) -> dict:
    """The cheap transparency payload: the per-facet multiplier chain on each axis (genre family /
    era decade / artist), the modes axis at all three timescales it exists on, lanes, breadth,
    sources, and the graduation funnel. Expensive panels (embedding/recall, playlist contexts,
    centroid tilt) are separate (engine_panel / centroid_tilt_panel), htmx-lazy on the page."""
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

    return {
        "genres": genres, "eras": eras, "artists": artists,
        "modes": mode_layers(store, now),
        "lanes": [{"name": n, "label": lbl, "help": h, "weight": weights.get(f"lane:{n}", 1.0)}
                  for n, lbl, h in rec_params.LANES],
        "breadth": bd["breadth"], "n_families": bd["n_families"],
        "breadth_word": _breadth_word(bd["breadth"]),
        # #85: no "freshness" key any more - the old sync-staleness relax of the whole transient read
        # is gone; each source in `sources` now fades independently on its own wall-clock half-life.
        "sources": _sources(store),
        "funnel": [{"facet": f, "score": s, "threshold": graduation_threshold,
                    "frac": max(-1.0, min(1.0, s / graduation_threshold))}
                   for f, s in sorted(theme.items(), key=lambda x: -abs(x[1]))],
        "facet_mult_min": fparams[1], "facet_mult_max": fparams[2],
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


def _family_centroids(store, keys, V, idx) -> dict:
    """{genre family: unit centroid} in the collaborative embedding space - the reference directions
    both free-vector layers are projected onto."""
    fam_keys: dict = {}
    tg = RecDao(store).track_genres(list(keys))
    for k in keys:
        if k in tg:
            fam = genre_map.family(tg[k])
            if fam:
                fam_keys.setdefault(fam, []).append(k)
    out = {}
    for fam, ks in fam_keys.items():
        rows = [idx[k] for k in ks if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        out[fam] = c / (np.linalg.norm(c) + 1e-9)
    return out


def centroid_tilt_panel(store, now, top=6) -> dict:
    """The embedding axis, read at the two timescales on which a free direction exists.

    SESSION (`layers.session_tilt`, hours half-life) and TRANSIENT (`transient.centroid_tilt`, days
    half-life) are both unit directions in the same collaborative space, so projecting each onto the
    same genre-family centroids puts them on one comparable -1..+1 scale: which sounds this sitting
    leans toward, against which sounds the last few weeks lean toward.

    NOW has no row here by design. An hour of listening is too few events to estimate a direction in
    embedding space without whiplash, so the NOW layer is categorical over taste modes instead (see
    layers.py); it appears in `mode_layers`, not here.
    """
    # The half-lives ride along unconditionally: the template names them in its explanatory copy,
    # which it renders whether or not either layer currently has a direction.
    panel = {"families": [], "has_session": False, "has_transient": False,
             "halflife_h": rec_params.get_param(store, "session_halflife_h"),
             "play_halflife_d": rec_params.get_param(store, "play_halflife_d")}
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return panel
    # Both already return unit directions (their wall-clock decay is baked in per-event, before
    # normalization). Re-normalizing is defensive, and keeps the projection an honest cosine.
    tilts = {"session": layers.session_tilt(store, now, V, idx),
             "transient": transient.centroid_tilt(store, now, V, idx)}
    unit = {k: (t / (np.linalg.norm(t) + 1e-9)) if t is not None else None for k, t in tilts.items()}
    if unit["session"] is None and unit["transient"] is None:
        return panel

    fams = []
    for fam, c in _family_centroids(store, keys, V, idx).items():
        row = {"name": fam}
        for layer, t in unit.items():
            row[layer] = float(c @ t) if t is not None else None
        fams.append(row)
    fams.sort(key=lambda r: -max(abs(r[k] or 0.0) for k in ("session", "transient")))
    panel.update(families=fams[:top],
                 has_session=unit["session"] is not None,
                 has_transient=unit["transient"] is not None)
    return panel
