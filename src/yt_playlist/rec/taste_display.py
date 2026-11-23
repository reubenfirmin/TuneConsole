"""Read-only transparency view of the recommendation model for the Taste page.

Pure calculations over supplied model, evidence and configuration values.

Listening styles show the NOW and TODAY balance alongside library coverage.
Facet weights show temporary steering, RECENT evidence and the PERMANENT baseline.
Recent sound compares the TODAY and RECENT track-scoring directions.
"""
import numpy as np

from yt_playlist.rec import transient_math as transient
from yt_playlist.rec.transient import source_age, source_confidence


def _axis_rows(prefix, shares, weights, standing, leans, fparams, session=None):
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
        active = (session or {}).get(token) if pw != 0. else None
        sl = standing.get(token, 1.0)
        tlean = leans.get(token, 0.0)
        tmult = transient.facet_multiplier(tlean, *fparams)
        rows.append({"name": name, "share": share,
                     "permanent_weight": pw, "standing_lean": sl, "lasting": pw * sl, "session_weight": active,
                     "transient_lean": tlean, "transient_mult": tmult,
                     "effective": (pw if active is None else active) * sl * tmult})
    return rows


def source_weights(weights, records, now, *, halflife, lane_definitions, lane_default, session=None):
    """Source controls without loading the facet, style, or learning diagnostics."""
    lanes = []
    for name, label, help_text in lane_definitions:
        record = records.get(f"lane:{name}")
        weight = weights.get(f"lane:{name}", lane_default)
        saved = record["weight"] if record else lane_default
        updated = record["updated_at"] if record else None
        lanes.append({"name": name, "label": label, "help": help_text, "weight": weight,
                      "session": f"lane:{name}" in (session or {}),
                      "saved_weight": saved, "has_record": record is not None,
                      "updated_at": updated,
                      "age_days": max(0.0, (now - updated) / 86400) if updated is not None else None,
                      "fade_delta": weight - saved})
    return {"lanes": lanes, "lane_weight_halflife_days": halflife}


def _graduation_change(entry):
    axis, _, name = entry["axis"].partition(":")
    if axis == "genre":
        name = name.removeprefix("other:")
    elif axis == "era" and name.isdigit():
        name += "s"
    source_labels = {"play": "Listening", "like": "Likes", "dislike": "Dislikes",
                     "mood": "Mood feedback", "vibe": "Mood feedback",
                     "feedback": "Recommendation feedback", "slider": "Held adjustments",
                     "dated-support": "Repeated listening and choices"}
    return {"id": entry["id"], "name": name or entry["axis"], "axis": axis,
            "direction": "boost" if entry["factor"] > 1 else "reduction" if entry["factor"] < 1 else "neutral",
            "factor": entry["factor"], "new_weight": entry["new_weight"],
            "source": source_labels.get(entry["source"], "Other feedback"),
            "updated_at": entry["created_at"]}


def overview(bd, *, modes, shares, n, current_modes, profiles=None, coverage, ratings, changes):
    """Only the three Overview readings; no training baskets, pools, or ranking diagnostics."""
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
                       "has_styles": bool(modes),
                       "coverage": coverage, "ratings": ratings,
                       "changes": [_graduation_change(entry) for entry in changes]}}


def _attach_graduation(rows, prefix, theme, thr):
    for r in rows:
        score = theme.get(f"{prefix}:{r['name']}", 0.0)
        r["graduation"] = {"score": score, "threshold": thr,
                           "frac": max(-1.0, min(1.0, score / thr)) if thr else 0.0}
    return rows


def _breadth_word(breadth):
    """Same thresholds the 'Taste breadth' card uses: >0.66 eclectic, <0.33 focused, else balanced."""
    return "eclectic" if breadth > 0.66 else ("focused" if breadth < 0.33 else "balanced")


def _artist_shares(alla, top=12):
    """Top artists by play share, normalized over ALL artists (not just the displayed top-N), so the
    shares are comparable to the genre/era shares, which are likewise full-population."""
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


def mode_layers(modes, *, now_shares, now_n, current_modes, session_shares, session_n,
                evidence, session_half, minimum) -> dict:
    """The modes axis, read at three timescales - the only axis on which NOW and SESSION exist.

    All three readings are shares over the SAME active-mode list, so they stack: PERMANENT is the
    durable shape of your taste, SESSION is this sitting's balance, NOW is the last few hours. NOW
    and SESSION are confidence-gated (`now_min_events` distinct played keys with a known sound);
    below the gate they report nothing rather than a weak guess, and `segments` is empty.
    """
    # The saved grouping remains meaningful when enrichment changes the content space. Do not
    # classify new plays against stale centroids, but keep that last library portrait visible.
    return {
        "modes": modes,
        "styles_current": bool(current_modes),
        "now": {"segments": _ribbon_segments(now_shares, modes), "n": now_n},
        "session": {"segments": _ribbon_segments(session_shares, modes), "n": session_n,
                    "halflife_h": session_half},
        "permanent": {"segments": _ribbon_segments(_permanent_mode_shares(modes), modes),
                      "n": sum(m["size"] for m in modes)},
        "min_events": int(minimum),
        "evidence": evidence,
    }


def _sources(now, *, mood_events, plays, likes, skips, half, weights):
    """#85: there is no longer a single rank-based recency alpha blending these sources - each one
    fades independently on its own wall-clock half-life (transient.decay_weight). Report those
    half-lives instead so the page states the real per-source decay, not a retired blend knob."""
    mood_pos = mood_neg = 0
    for _ts, direction, _keys in mood_events:
        if direction > 0:
            mood_pos += 1
        elif direction < 0:
            mood_neg += 1

    def mass(pairs, source):
        return sum(weights[source] * source_confidence(ts) * transient.decay_weight(source_age(now, ts), half[source])
                   for _k, ts in pairs)

    source_pairs = {
        "mood": [("", ts) for ts, direction, _keys in mood_events if direction],
        "play": plays, "like": likes, "skip": skips,
    }
    latest = min((source_age(now, ts) for pairs in source_pairs.values() for _k, ts in pairs), default=None)
    return {
        "mood_pos": mood_pos, "mood_neg": mood_neg,
        "plays": len(plays), "likes": len(likes), "skips": len(skips),
        "halflife_days": half,
        "effective": {source: mass(pairs, source) for source, pairs in source_pairs.items()},
        "dated_plays": sum(source_confidence(ts) < 1. for _key, ts in plays),
        "latest_age_h": max(0.0, latest / 3600.0) if latest is not None else None,
    }


def learning_progress(now, *, threshold, entries, sources, choices, weight_sources, changes, lane_max, dated=False) -> dict:
    """Learning readings from saved evidence, without vectors or style classification.

    Keep every ledger entry available for searching. Progress measures the next threshold
    crossing, not a taste score or a share of listening. The ledger itself does not decay.
    """
    groups = {axis: {"axis": axis, "title": title, "rows": []} for axis, title in
              (("artist", "Artists"), ("genre", "Genres"), ("era", "Eras"))}
    for entry in entries:
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
            "percent": int(entry['progress'] * 100) if dated else min(100, int(abs(score) / threshold * 100)),
            "direction": "boost" if score > 0 else "reduction" if score < 0 else "neutral",
            "score": score, "updated_at": entry["updated_at"],
            **({name: entry[name] for name in ('days', 'deliberate_days', 'last_day')} if dated else {}),
        })
    for group in groups.values():
        group["rows"].sort(key=lambda row: (-row["updated_at"],
                                            row["name"].casefold(), row["facet"]))

    activity = []
    for key, title, count in (("play", "Plays", sources["plays"]),
                              ("like", "Likes", sources["likes"]),
                              ("mood", "Mood choices", sources["mood_pos"] + sources["mood_neg"]),
                              ("skip", "Skips", sources["skips"])):
        half = sources["halflife_days"][key]
        points = [(i * 200 / 56, 64 - 60 * transient.decay_weight(i * .5 * 86400, half))
                  for i in range(57)]
        curve = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
        activity.append({"key": key, "title": title, "count": count, "half_days": half,
                         "curve": curve, "fill": curve + " L 200 64 L 0 64 Z"})
    weight_sources = {**weight_sources, 'lanes': [dict(row) for row in weight_sources['lanes']]}
    weight_sources["lanes"].sort(key=lambda row: (row["updated_at"] is None,
                                                -(row["updated_at"] or 0), row["label"]))
    weight_sources["maximum"] = lane_max
    changes = [_graduation_change(entry) for entry in changes]
    return {"groups": list(groups.values()), "activity": activity, "dated": dated, "dated_plays": sources.get('dated_plays', 0),
            "choices": {"rows": choices, "total": len(choices),
                        "offered": sum(row["offered"] for row in choices),
                        "picked": sum(row["picked"] for row in choices)},
            "source_weights": weight_sources, "changes": changes,
            "latest_age_h": sources["latest_age_h"]}


def model_transparency(*, sources, weights, standing, leans, theme, fparams, graduation_threshold,
                       bd, era_shares, artist_shares, modes, activity_sources, session=None, support=None) -> dict:
    """The cheap transparency payload: the per-facet multiplier chain on each axis (genre family /
    era decade / artist), the modes axis at all three timescales it exists on, lanes, breadth,
    sources, and the graduation funnel. Expensive panels (embedding/recall, playlist contexts,
    centroid tilt) are separate (playlist_influence_panel / centroid_tilt_panel), htmx-lazy on the page."""
    fam_shares = sorted(bd["families"].items(), key=lambda x: -x[1])
    genres = _attach_graduation(_axis_rows("genre", fam_shares, weights, standing, leans, fparams, session),
                                "genre", theme, graduation_threshold)
    eras = _attach_graduation(
        _axis_rows("era", era_shares, weights, standing, leans, fparams, session),
        "era", theme, graduation_threshold)
    artists = _attach_graduation(
        _axis_rows("artist", artist_shares, weights, standing, leans, fparams, session),
        "artist", theme, graduation_threshold)

    return {
        "genres": genres, "eras": eras, "artists": artists,
        "modes": modes,
        **sources,
        "breadth": bd["breadth"], "n_families": bd["n_families"],
        "breadth_word": _breadth_word(bd["breadth"]),
        # #85: no "freshness" key any more - the old sync-staleness relax of the whole transient read
        # is gone; each source in `sources` now fades independently on its own wall-clock half-life.
        "sources": activity_sources,
        "routing": [
            {"layer": "NOW", "evidence": "active-session plays, choices, and manual steering",
             "consumers": "adjusts listening-style choices and track scores"},
            {"layer": "TODAY", "evidence": "timestamped plays and choices since local midnight",
             "consumers": "changes track order within recommendations"},
            {"layer": "RECENT", "evidence": "plays, choices, likes, mood feedback, and skips",
             "consumers": "adjusts similarity scores and genre, era, and artist rankings"},
            {"layer": "PERMANENT", "evidence": "sustained listening, deliberate returns, and likes across days",
             "consumers": "sets the starting taste match, discovery, and category preferences"},
        ],
        "dated_support": support is not None,
        "funnel": ([{"facet": f, **row, "frac": row["progress"]}
                    for f, row in sorted(support.items(), key=lambda item: (-item[1]["support"], item[0]))]
                   if support is not None else [{"facet": f, "score": s, "threshold": graduation_threshold,
                    "frac": max(-1.0, min(1.0, s / graduation_threshold))}
                   for f, s in sorted(theme.items(), key=lambda x: -abs(x[1]))]),
        "facet_mult_min": fparams[1], "facet_mult_max": fparams[2],
    }


def engine_panel(count, dimension, method, *, default_dimension) -> dict:
    """The track similarity model's technical setup, without loading vectors or taste contexts."""
    return {"vectors": count, 'dim': int(dimension or default_dimension), 'method': method or 'auto'}


def playlist_influence_panel(pt, stats, genres_by_pid, axes_by_pid, top=8) -> dict:
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
    if not pt:
        return panel
    pids = [pt.pids[i] if i < len(pt.pids) else None for i in range(len(pt.titles))]
    sources = getattr(pt, "sources", [{"kind": "playlist" if pid is not None else "history"} for pid in pids])
    order = sorted((i for i, weight in enumerate(pt.weights) if weight > 0),
                   key=lambda i: (-float(pt.weights[i]), pt.titles[i].casefold()))
    playlist_total = sum(pids[i] is not None for i in order)
    basis = ("listening" if not playlist_total else
             "plays" if any(stats.get(pid, (None, 0))[1] for pid in pids) else "equal")
    panel.update(contexts_total=len(pt.titles), active_total=len(order), playlist_total=playlist_total,
                 unplayed_total=len(pt.titles) - len(order), basis=basis,
                 top_count=min(3, len(order)), top_share=share_label(sum(float(pt.weights[i]) for i in order[:3])))
    if any(source["kind"] not in {"playlist", "history"} for source in sources):
        panel["basis"] = "interests"
    offset = 0.0
    for i in order[:top]:
        pid, weight = pids[i], float(pt.weights[i])
        raw_genres = genres_by_pid.get(pid, [])
        genre_axes = axes_by_pid.get(pid, {})
        families = Counter(genre_axes[g][0] for g in raw_genres if g)
        genres = [name for name, _ in sorted(families.items(), key=lambda row: (-row[1], row[0]))[:2]]
        context = {"pid": pid, "title": pt.titles[i], "weight": weight,
                   "percent": weight * 100, "percent_label": share_label(weight), "offset": offset,
                   "genre": genres[0] if genres else "", "genres": genres,
                   "listens": stats.get(pid, (None, 0))[1] if pid is not None else None,
                   "is_other": False, "source": sources[i]}
        panel["contexts"].append(context)
        offset += weight * 100
    panel["segments"] = list(panel["contexts"])
    remaining = order[top:]
    if remaining:
        weight = sum(float(pt.weights[i]) for i in remaining)
        noun = "playlist" if all(pids[i] is not None for i in remaining) else "interest"
        title = f"Other {noun}" if len(remaining) == 1 else f"Other {len(remaining)} {noun}s"
        panel["other"] = {"title": title, "weight": weight,
                          "percent": weight * 100, "percent_label": share_label(weight), "offset": offset,
                          "listens": sum(stats.get(pids[i], (None, 0))[1] for i in remaining),
                          "is_other": True, "pid": None, "genres": []}
        panel["segments"].append(panel["other"])
    return panel


def _family_centroids(keys, V, idx, *, genres, genre_axes) -> dict:
    """{genre family: unit centroid} in the collaborative embedding space - the reference directions
    both free-vector layers are projected onto."""
    fam_keys: dict = {}
    tg = genres
    for k in keys:
        if k in tg:
            fam = genre_axes.get(tg[k], ('', None))[0]
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


def centroid_tilt_panel(keys, V, idx, *, tilts, genres, genre_axes, halflife_h, play_halflife_d, top=6) -> dict:
    """Compare the TODAY and RECENT scoring tilts in the collaborative space."""
    # The half-lives ride along unconditionally: the template names them in its explanatory copy,
    # which it renders whether or not either layer currently has a direction.
    panel = {"families": [], "has_session": False, "has_transient": False,
             "halflife_h": halflife_h, 'play_halflife_d': play_halflife_d}
    if V is None:
        return panel
    # Keep each tilt's magnitude: normalizing again would hide decay from the live model display.
    if tilts["session"] is None and tilts["transient"] is None:
        return panel

    fams = []
    for fam, c in _family_centroids(keys, V, idx, genres=genres, genre_axes=genre_axes).items():
        row = {"name": fam}
        for layer, t in tilts.items():
            row[layer] = float(c @ t) if t is not None else None
        fams.append(row)
    fams.sort(key=lambda r: -max(abs(r[k] or 0.0) for k in ("session", "transient")))
    panel.update(families=fams[:top],
                 has_session=tilts["session"] is not None,
                 has_transient=tilts["transient"] is not None)
    return panel
