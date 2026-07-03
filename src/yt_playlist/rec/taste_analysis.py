"""Taste analysis: read-only summaries of the library's taste, covering breadth, palette, era spread,
the Home fingerprint, and per-playlist facets/diversity. A leaf below scoring (imports no scoring)."""
import math
import statistics

from yt_playlist.util import genre_map
from yt_playlist.rec import rec_params, transient
from yt_playlist.rec.rec_dao import RecDao


def era_distribution(store) -> list:
    """Decades present in the library, by play-weighted share, most-prominent first."""
    dist = RecDao(store).era_play_distribution()
    total = sum(dist.values())
    if not total:
        return []
    return sorted(((d, c / total) for d, c in dist.items()), key=lambda x: -x[1])


def taste_fingerprint(store, now) -> dict:
    """Compact, legible 'you right now' for the Home header: top genre families, breadth, era lean.

    Each family/era carries its current steering weight so the header can render a draggable bar.
    Also includes 'effective' = permanent x standing lean, clamped to [GENRE_MIN, GENRE_MAX], which
    is what the slider THUMB binds to (the held steer the user experiences), plus 'live' = effective x
    the live transient facet multiplier (recent plays/likes/dislikes/mood), clamped the same way, which
    a separate MARKER binds to. Splitting them keeps the two independent mood signals (explicit steer
    vs. listen behavior) from overloading one bar position. 'live_active' is False when there is no
    meaningful live signal (e.g. a stale sync), so the bar can hide the marker.

    Pinned axes: 'pinned' is True for any family/era that has an explicit stored lean (set via
    /home/fingerprint/add or by steering its bar). The Home column always renders the top-by-share
    bars PLUS every pinned one, so an added bar shows even when it's a low-share PLAYED family (e.g.
    'rock-post', the family post-rock lives under) that ranks past the top few, not just a zero-play
    niche. A pinned axis not in the play distribution at all is appended with share=0.0.
    """
    bd = taste_breadth(store)
    w = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    leans = store.get_leans()
    hidden = store.hidden_facets()                                   # bars the user removed (display-only)
    tleans = transient.facet_leans(store, now)                       # live transient (plays+likes+mood+dislikes)
    fgain = rec_params.get_param(store, "facet_gain")
    fmin = rec_params.get_param(store, "facet_mult_min")
    fmax = rec_params.get_param(store, "facet_mult_max")

    def _live(axis, effective):
        """Where the live transient model puts this axis right now: effective x facet multiplier,
        clamped to the bar's [GENRE_MIN, GENRE_MAX] scale. 'active' is False when there is no
        meaningful live lean (so the bar can hide the marker)."""
        lean = tleans.get(axis, 0.0)
        tmult = transient.facet_multiplier(lean, fgain, fmin, fmax)
        live = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, effective * tmult))
        return live, abs(lean) > 1e-6

    def _subs(fam):
        """A family's drill-down members: its subgenres MINUS the family token itself (no self-dupe),
        minus any the user hid, minus any that already carry a lean: a leaned/added subgenre is
        promoted to its own top-level bar, so it never shows in both places."""
        out = []
        for sub in genre_map.subgenres_of(fam):
            ax = f"genre:{sub}"
            if sub == fam or ax in hidden or ax in leans:
                continue
            eff = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, w.get(ax, 1.0) * store.get_lean(ax)))
            live, active = _live(ax, eff)
            out.append({"name": sub, "axis": ax, "effective": eff, "live": live, "live_active": active})
        return out

    families = []
    for f, share in sorted(bd["families"].items(), key=lambda x: -x[1]):
        if f"genre:{f}" in hidden:
            continue
        ax = f"genre:{f}"
        eff = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, w.get(ax, 1.0) * store.get_lean(ax)))
        live, active = _live(ax, eff)
        subs = _subs(f)
        families.append({"name": f, "share": share, "weight": w.get(ax, 1.0),
                         "effective": eff, "live": live, "live_active": active,
                         "pinned": ax in leans, "subgenres": subs, "expandable": bool(subs)})
    eras = []
    for d, share in era_distribution(store):
        if f"era:{d}" in hidden:
            continue
        ax = f"era:{d}"
        eff = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, w.get(ax, 1.0) * store.get_lean(ax)))
        live, active = _live(ax, eff)
        eras.append({"name": d, "share": share, "weight": w.get(ax, 1.0),
                     "effective": eff, "live": live, "live_active": active, "pinned": ax in leans})

    # Append pinned axes stored in leans but not present in the play-distribution lists at all.
    known_genre_names = {entry["name"] for entry in families}
    known_era_names = {entry["name"] for entry in eras}
    for axis, lean_val in leans.items():
        if axis in hidden:
            continue                                                 # removed from the panel; don't re-surface
        if axis.startswith("genre:"):
            name = axis[len("genre:"):]
            if name not in known_genre_names:
                weight = w.get(axis, 1.0)
                effective = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, weight * lean_val))
                live, active = _live(axis, effective)
                subs = _subs(name)
                families.append({"name": name, "share": 0.0, "weight": weight,
                                 "effective": effective, "live": live, "live_active": active,
                                 "pinned": True, "subgenres": subs, "expandable": bool(subs)})
                known_genre_names.add(name)
        elif axis.startswith("era:"):
            name = axis[len("era:"):]
            if name not in known_era_names:
                weight = w.get(axis, 1.0)
                effective = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, weight * lean_val))
                live, active = _live(axis, effective)
                eras.append({"name": name, "share": 0.0, "weight": weight,
                             "effective": effective, "live": live, "live_active": active, "pinned": True})
                known_era_names.add(name)

    # breadth = the *measured* spread (a backdrop the bar shows); breadth_bias = the user's current
    # focused<->eclectic steer that the draggable handle binds to (#7). has_steering gates the "Reset to
    # default" affordance: true whenever the user has steered (a lean), removed a bar (hidden), or moved
    # breadth off-center, i.e. there's something to reset.
    bias = rec_params.get_param(store, "breadth_bias")
    return {"families": families, "eras": eras, "breadth": bd["breadth"],
            "breadth_bias": bias,
            "has_steering": bool(leans) or bool(hidden) or bias != 0.0}


def playlist_facets(store, playlist_id) -> dict:
    """The mix's facets for the transient feedback panel: the genres, eras, and tracks present, each
    with the identity_keys to tilt toward/away. Genres are by descending presence; eras chronological.
    """
    keys = store.get_playlist_track_keys(playlist_id)
    dao = RecDao(store)
    meta = store.tracks_by_keys(keys)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)
    fam_keys, era_keys = {}, {}
    for k in keys:
        if k in genres:
            fam_keys.setdefault(genre_map.family(genres[k]), []).append(k)
        if k in decades:
            era_keys.setdefault(decades[k], []).append(k)
    return {
        "genres": [{"name": f, "keys": ks} for f, ks in sorted(fam_keys.items(), key=lambda x: -len(x[1]))],
        "eras": [{"name": d, "keys": ks} for d, ks in sorted(era_keys.items())],
        "tracks": [{"key": k, "title": meta[k]["title"], "artist": meta[k]["artist"]}
                   for k in keys if k in meta],
    }


def playlist_mood_state(store, playlist_id, now) -> int:
    """+1/-1 if there's an active *whole-mix* mood for this playlist, else 0. Persists until you change
    it (the transient signal no longer expires on a clock)."""
    keys = set(store.get_playlist_track_keys(playlist_id))
    if not keys:
        return 0
    for _created, direction, mkeys in RecDao(store).recent_mood_events():   # newest-first
        if set(mkeys) == keys:                       # whole-mix feedback; first match is the latest
            return 1 if direction > 0 else -1
    return 0


def track_mood_states(store, now) -> dict:
    """Map identity_key -> +1/-1 for *per-track* mood signals (the "🔥 More / 🙅 Less like this" row
    levers), so a generated playlist can flag which rows you nudged. Those buttons seed a single key,
    so single-key events are exactly the per-track ones. Whole-mix and facet tilts (many keys) are
    excluded. Persists until changed; newest-first, so the first event seen for a key is the latest."""
    out = {}
    for _created, direction, mkeys in RecDao(store).recent_mood_events():   # newest-first
        if len(mkeys) == 1 and mkeys[0] not in out:    # first match is the latest -> latest wins
            out[mkeys[0]] = 1 if direction > 0 else -1
    return out


def taste_breadth(store) -> dict:
    """How narrow vs eclectic this library is, from the entropy of its genre-family mix.

    breadth in [0,1]: ~0 = one-vibe (opera-only), ~1 = spread across many families. Computed over
    a *play-weighted* genre distribution, so a low-play context doesn't inflate your apparent breadth
    or register its genres as 'in palette' (the Clapton-leak fix). Spec §5.2.
    """
    fams: dict = {}
    for genre, c in RecDao(store).genre_play_distribution().items():
        fams[genre_map.family(genre)] = fams.get(genre_map.family(genre), 0) + c
    total = sum(fams.values())
    if total == 0 or len(fams) <= 1:
        return {"breadth": 0.0, "n_families": len(fams),
                "families": {f: 1.0 for f in fams}, "n_tagged": total}
    shares = {f: c / total for f, c in fams.items()}
    entropy = -sum(p * math.log(p) for p in shares.values())
    return {"breadth": entropy / math.log(len(fams)), "n_families": len(fams),
            "families": shares, "n_tagged": total}


def palette(store):
    """The library's genre-family palette + an out-of-palette penalty for a candidate genre.

    fit(genre) = 0 if its family is already present; otherwise the nearest-present-family
    distance scaled by breadth: 'absence-as-avoidance', a broad library that has never adopted
    a family penalizes it more (deliberate exclusion), a narrow one less (merely unexplored).
    Spec §5.3.
    """
    bd = taste_breadth(store)
    present = set(bd["families"])

    def fit(genre):
        fam = genre_map.family(genre)
        if not present or fam in present:
            return 0.0
        nearest = min(genre_map.family_distance(fam, p) for p in present)
        return nearest * (rec_params.get_param(store, "palette_absence_penalty") + bd["breadth"])

    return {"breadth": bd["breadth"], "present": present, "fit": fit}


def playlist_genre_diversity(store, playlist_id):
    """How genre-tight vs genre-wide a playlist is, via pairwise genre-map distances.

    Returns {min, max, median, n_tagged} over its tagged tracks, or None if fewer than two
    are tagged. median≈0 = tight (one vibe); median≈1 = eclectic. Spec §6.B.
    """
    genres = store.playlist_track_genres(playlist_id)
    if len(genres) < 2:
        return None
    dists = [genre_map.distance(genres[i], genres[j])
             for i in range(len(genres)) for j in range(i + 1, len(genres))]
    return {"min": min(dists), "max": max(dists),
            "median": statistics.median(dists), "n_tagged": len(genres)}
