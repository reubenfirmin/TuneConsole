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
from yt_playlist.rec import embed, layers, mix_tuning, mode_eval, mode_profiles, rec_params, recommend, surfaces, transient
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


def mix_choices(store):
    """Choice evidence with descriptions of the groups those choices actually train."""
    evidence = mode_eval.choice_evidence(store)
    profiles = mode_profiles.describe_modes(store)
    for row in evidence["rows"]:
        row["profile"] = profiles[row["mode_id"]]
    return evidence


def generated_playlists(store, now):
    """Generation facts from prepared pools and saved settings; never rebuild a Home mix."""
    bundles = store.get_proposals("mode_bundles") or {}
    blocked = store.suppressed_keys("for_you", now) | set(RecDao(store).generated_track_keys())
    muted = store.muted_artists()
    examples = {}
    for source in ("wheelhouse", "explore", "fresh", "comfort"):
        examples[source] = next((t for t in bundles.get("all", {}).get(source, [])
                                 if t.get("key") not in blocked and t.get("artist") not in muted), None)
    gp = rec_params.get_param
    return {"choices": mix_choices(store), "examples": examples,
            "taste_rank_ready": bool(surfaces.playlist_taste(store)),
            "catalog_content_percent": round(surfaces.CATALOG_CONTENT_FRAC * 100),
            "comfort_min_plays": gp(store, "comfort_min_plays"),
            "comfort_recency_days": gp(store, "comfort_recency_full_days"),
            "artist_cap": gp(store, "modes_artist_cap"),
            "album_cap": gp(store, "modes_album_cap"),
            "fresh_artist_cap": gp(store, "fresh_artist_cap")}


def source_weights(store, now):
    """Source controls without loading the facet, style, or learning diagnostics."""
    mix_tuning.expire(store, now)
    halflife = rec_params.get_param(store, "weight_revert_halflife_d")
    weights = store.get_weights(now=now, revert_halflife_d=halflife)
    records = store.weight_records()
    lanes = []
    for name, label, help_text in rec_params.LANES:
        record = records.get(f"lane:{name}")
        weight = weights.get(f"lane:{name}", rec_params.LANE_DEFAULT)
        saved = record["weight"] if record else rec_params.LANE_DEFAULT
        updated = record["updated_at"] if record else None
        lanes.append({"name": name, "label": label, "help": help_text, "weight": weight,
                      "saved_weight": saved, "has_record": record is not None,
                      "updated_at": updated,
                      "age_days": max(0.0, (now - updated) / 86400) if updated is not None else None,
                      "fade_delta": weight - saved})
    return {"lanes": lanes, "lane_weight_halflife_days": halflife}


def overview(store, now, profiles=None):
    """Only the three Overview readings; no training baskets, pools, or ranking diagnostics."""
    bd = recommend.taste_breadth(store)
    shares, n, current_modes = layers.session_mode_mix(store, now)
    modes = store.modes.list_modes(active_only=True)
    library = _permanent_mode_shares(modes)
    shifts = []
    if shares:
        for color, mode in enumerate(modes):
            mid = mode["mode_id"]
            recent, saved = shares.get(mid, 0.0), library.get(mid, 0.0)
            label = profiles[mid]["title"] if profiles and mid in profiles else mode["label"]
            shifts.append({"label": label, "color_idx": color,
                           "recent": recent, "library": saved, "delta": recent - saved})
    shifts.sort(key=lambda r: -abs(r["delta"]))
    return {"genres": [{"name": name, "share": share} for name, share in
                       sorted(bd["families"].items(), key=lambda x: -x[1])],
            "breadth_word": _breadth_word(bd["breadth"]),
            "recent": {"shifts": shifts[:3], "n": n, "styles_current": bool(current_modes),
                       "has_styles": bool(modes), "evidence": layers.evidence_coverage(store, now)["session"],
                       "min_events": int(rec_params.get_param(store, "now_min_events"))}}


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
    now_shares, now_n, current_modes = layers.now_mode_mix(store, now)
    session_shares, session_n, _ = layers.session_mode_mix(store, now)
    evidence = layers.evidence_coverage(store, now)
    # The saved grouping remains meaningful when enrichment changes the content space. Do not
    # classify new plays against stale centroids, but keep that last library portrait visible.
    modes = store.modes.list_modes(active_only=True)
    return {
        "modes": modes,
        "styles_current": bool(current_modes),
        "now": {"segments": _ribbon_segments(now_shares, modes), "n": now_n,
                "window_h": rec_params.get_param(store, "now_window_h")},
        "session": {"segments": _ribbon_segments(session_shares, modes), "n": session_n,
                    "halflife_h": rec_params.get_param(store, "session_halflife_h")},
        "permanent": {"segments": _ribbon_segments(_permanent_mode_shares(modes), modes),
                      "n": sum(m["size"] for m in modes)},
        "min_events": int(rec_params.get_param(store, "now_min_events")),
        "evidence": evidence,
    }


def _sources(store, now):
    """#85: there is no longer a single rank-based recency alpha blending these sources - each one
    fades independently on its own wall-clock half-life (transient.decay_weight). Report those
    half-lives instead so the page states the real per-source decay, not a retired blend knob."""
    mood_pos = mood_neg = 0
    mood_events = store.recent_mood_events()
    for _ts, direction, _keys in mood_events:
        if direction > 0:
            mood_pos += 1
        elif direction < 0:
            mood_neg += 1
    limit = rec_params.get_param(store, "recent_play_limit")
    gp = rec_params.get_param
    plays = transient.evidence_plays(store, limit)
    likes = store.recent_liked_with_ts(limit=limit)
    dislikes = store.disliked_with_ts()
    skip_hl = gp(store, "skip_halflife_d")
    skips = store.recent_skips_with_ts(now - 4 * skip_hl * 86400)
    half = {"mood": gp(store, "mood_halflife_d"), "play": gp(store, "play_halflife_d"),
            "like": gp(store, "like_halflife_d"), "dislike": gp(store, "dislike_halflife_d"),
            "skip": skip_hl}
    weights = {"mood": 1.0, "play": gp(store, "play_transient_w"),
               "like": gp(store, "like_transient_w"), "dislike": gp(store, "dislike_transient_w"),
               "skip": gp(store, "skip_transient_w")}

    def mass(pairs, source):
        return sum(weights[source] * transient.decay_weight(now - ts, half[source]) for _k, ts in pairs)

    source_pairs = {
        "mood": [("", ts) for ts, direction, _keys in mood_events if direction],
        "play": plays, "like": likes, "dislike": dislikes, "skip": skips,
    }
    latest = max((ts for pairs in source_pairs.values() for _k, ts in pairs), default=None)
    return {
        "mood_pos": mood_pos, "mood_neg": mood_neg,
        "plays": len(plays), "likes": len(likes), "dislikes": len(dislikes), "skips": len(skips),
        "halflife_days": half,
        "effective": {source: mass(pairs, source) for source, pairs in source_pairs.items()},
        "latest_age_h": max(0.0, (now - latest) / 3600.0) if latest is not None else None,
    }


def learning_progress(store, now) -> dict:
    """Learning readings from saved evidence, without vectors or style classification.

    Keep every ledger entry available for searching. Progress measures the next threshold
    crossing, not a taste score or a share of listening. The ledger itself does not decay.
    """
    threshold = rec_params.get_param(store, "theme_threshold")
    groups = {axis: {"axis": axis, "title": title, "rows": []} for axis, title in
              (("artist", "Artists"), ("genre", "Genres"), ("era", "Eras"))}
    for entry in store.theme_rows():
        axis, separator, name = entry["facet"].partition(":")
        if not separator:
            axis, name = "other", entry["facet"]
        if axis not in groups:
            groups[axis] = {"axis": axis, "title": "Recommendation categories" if axis == "lane"
                            else "Other preferences", "rows": []}
        if axis == "genre":
            name = name.removeprefix("other:")
        elif axis == "era" and name.isdigit():
            name += "s"
        score = entry["score"]
        groups[axis]["rows"].append({
            "facet": entry["facet"], "name": name,
            "percent": min(100, int(abs(score) / threshold * 100)),
            "direction": "boost" if score > 0 else "reduction" if score < 0 else "neutral",
            "score": score, "updated_at": entry["updated_at"],
        })
    for group in groups.values():
        group["rows"].sort(key=lambda row: (-row["updated_at"],
                                            row["name"].casefold(), row["facet"]))

    sources = _sources(store, now)
    activity = []
    for key, title, count in (("play", "Plays", sources["plays"]),
                              ("like", "Likes", sources["likes"]),
                              ("mood", "Mood choices", sources["mood_pos"] + sources["mood_neg"]),
                              ("dislike", "Dislikes", sources["dislikes"]),
                              ("skip", "Skips", sources["skips"])):
        half = sources["halflife_days"][key]
        points = [(i * 200 / 56, 64 - 60 * transient.decay_weight(i * .5 * 86400, half))
                  for i in range(57)]
        curve = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
        activity.append({"key": key, "title": title, "count": count, "half_days": half,
                         "curve": curve, "fill": curve + " L 200 64 L 0 64 Z"})
    choices = store.modes.choice_learning_rows()
    weight_sources = source_weights(store, now)
    weight_sources["lanes"].sort(key=lambda row: (row["updated_at"] is None,
                                                -(row["updated_at"] or 0), row["label"]))
    weight_sources["maximum"] = rec_params.LANE_MAX
    changes = []
    source_labels = {"play": "Listening", "like": "Likes", "dislike": "Dislikes",
                     "mood": "Mood feedback", "vibe": "Mood feedback",
                     "feedback": "Recommendation feedback", "slider": "Held adjustments"}
    for entry in store.recent_graduations(limit=50):
        axis, _, name = entry["axis"].partition(":")
        if axis == "genre":
            name = name.removeprefix("other:")
        elif axis == "era" and name.isdigit():
            name += "s"
        changes.append({"id": entry["id"], "name": name or entry["axis"], "axis": axis,
                        "direction": "boost" if entry["factor"] > 1 else "reduction" if entry["factor"] < 1 else "neutral",
                        "factor": entry["factor"], "new_weight": entry["new_weight"],
                        "source": source_labels.get(entry["source"], "Other feedback"),
                        "updated_at": entry["created_at"]})
    return {"groups": list(groups.values()), "activity": activity,
            "choices": {"rows": choices, "total": len(choices),
                        "offered": sum(row["offered"] for row in choices),
                        "picked": sum(row["picked"] for row in choices)},
            "source_weights": weight_sources, "changes": changes,
            "latest_age_h": sources["latest_age_h"]}


def model_transparency(store, now) -> dict:
    """The cheap transparency payload: the per-facet multiplier chain on each axis (genre family /
    era decade / artist), the modes axis at all three timescales it exists on, lanes, breadth,
    sources, and the graduation funnel. Expensive panels (embedding/recall, playlist contexts,
    centroid tilt) are separate (playlist_influence_panel / centroid_tilt_panel), htmx-lazy on the page."""
    sources = source_weights(store, now)
    weights = store.get_weights(now=now, revert_halflife_d=sources["lane_weight_halflife_days"])
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
        **sources,
        "breadth": bd["breadth"], "n_families": bd["n_families"],
        "breadth_word": _breadth_word(bd["breadth"]),
        # #85: no "freshness" key any more - the old sync-staleness relax of the whole transient read
        # is gone; each source in `sources` now fades independently on its own wall-clock half-life.
        "sources": _sources(store, now),
        "routing": [
            {"layer": "CURRENT", "evidence": "recent plays, once enough tracks can be matched",
             "consumers": "chooses the leading listening-style card on Home"},
            {"layer": "SESSION", "evidence": "plays from the last 24 hours, with older plays counting less",
             "consumers": "changes track order within recommendations"},
            {"layer": "RECENT", "evidence": "plays, likes, dislikes, mood choices, and skips",
             "consumers": "adjusts similarity scores and genre, era, and artist rankings"},
            {"layer": "SAVED", "evidence": "your library, trained model, and learned weights",
             "consumers": "sets the starting taste match, discovery, and category preferences"},
            {"layer": "LEARNING", "evidence": "repeated listening and deliberate adjustments",
             "consumers": "gradually updates saved preference weights"},
        ],
        "funnel": [{"facet": f, "score": s, "threshold": graduation_threshold,
                    "frac": max(-1.0, min(1.0, s / graduation_threshold))}
                   for f, s in sorted(theme.items(), key=lambda x: -abs(x[1]))],
        "facet_mult_min": fparams[1], "facet_mult_max": fparams[2],
    }


def engine_panel(store) -> dict:
    """The track similarity model's technical setup, without loading vectors or taste contexts."""
    return {"vectors": store.rec_vectors_count(),
            "dim": int(store.get_setting("rec_dim") or embed.DIM),
            "method": store.get_setting("rec_embed_method") or "auto"}


def playlist_influence_panel(store, top=8) -> dict:
    """Actual taste-model shares, their listening evidence, and the undisplayed remainder.

    Keep the denominator across ALL contexts: a shortened ranking must still describe the whole
    blend. Zero-weight playlists don't contribute, and the behavior-only fallback isn't a playlist.
    """
    from collections import Counter

    def share_label(weight):
        return "<0.1%" if 0 < weight < .001 else f"{weight * 100:.1f}".removesuffix(".0") + "%"

    panel = {"contexts": [], "segments": [], "contexts_total": 0, "active_total": 0,
             "playlist_total": 0, "unplayed_total": 0, "basis": "equal", "top_count": 0,
             "top_share": "0%", "other": None}
    pt = recommend.playlist_taste(store)
    if not pt:
        return panel
    pids = [pt.pids[i] if i < len(pt.pids) else None for i in range(len(pt.titles))]
    stats = store.get_playlist_listen_stats()
    order = sorted((i for i, weight in enumerate(pt.weights) if weight > 0),
                   key=lambda i: (-float(pt.weights[i]), pt.titles[i].casefold()))
    playlist_total = sum(pids[i] is not None for i in order)
    basis = ("listening" if not playlist_total else
             "plays" if any(stats.get(pid, (None, 0))[1] for pid in pids) else "equal")
    panel.update(contexts_total=len(pt.titles), active_total=len(order), playlist_total=playlist_total,
                 unplayed_total=len(pt.titles) - len(order), basis=basis,
                 top_count=min(3, len(order)), top_share=share_label(sum(float(pt.weights[i]) for i in order[:3])))
    offset = 0.0
    for i in order[:top]:
        pid, weight = pids[i], float(pt.weights[i])
        families = (Counter(genre_map.family(g) for g in store.playlist_track_genres(pid) if g)
                    if pid is not None else Counter())
        genres = [name for name, _ in sorted(families.items(), key=lambda row: (-row[1], row[0]))[:2]]
        context = {"pid": pid, "title": pt.titles[i], "weight": weight,
                   "percent": weight * 100, "percent_label": share_label(weight), "offset": offset,
                   "genre": genres[0] if genres else "", "genres": genres,
                   "listens": stats.get(pid, (None, 0))[1] if pid is not None else None,
                   "is_other": False}
        panel["contexts"].append(context)
        offset += weight * 100
    panel["segments"] = list(panel["contexts"])
    remaining = order[top:]
    if remaining:
        weight = sum(float(pt.weights[i]) for i in remaining)
        title = "Other playlist" if len(remaining) == 1 else f"Other {len(remaining)} playlists"
        panel["other"] = {"title": title, "weight": weight,
                          "percent": weight * 100, "percent_label": share_label(weight), "offset": offset,
                          "listens": sum(stats.get(pids[i], (None, 0))[1] for i in remaining),
                          "is_other": True, "pid": None, "genres": []}
        panel["segments"].append(panel["other"])
    return panel


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
    half-life) are bounded tilts in the same collaborative space. Projecting each onto the same
    genre-family centroids puts them on one comparable -1..+1 scale, including the fading strength
    used by scoring: which sounds this sitting leans toward, against the last few weeks' lean.

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
    # Keep each tilt's magnitude: normalizing again would hide decay from the live model display.
    tilts = {"session": layers.session_tilt(store, now, V, idx),
             "transient": transient.centroid_tilt(store, now, V, idx)}
    if tilts["session"] is None and tilts["transient"] is None:
        return panel

    fams = []
    for fam, c in _family_centroids(store, keys, V, idx).items():
        row = {"name": fam}
        for layer, t in tilts.items():
            row[layer] = float(c @ t) if t is not None else None
        fams.append(row)
    fams.sort(key=lambda r: -max(abs(r[k] or 0.0) for k in ("session", "transient")))
    panel.update(families=fams[:top],
                 has_session=tilts["session"] is not None,
                 has_transient=tilts["transient"] is not None)
    return panel
