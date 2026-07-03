"""#88 The layered taste model: several readings of "what does this listener want", each at its own
timescale, from fastest (this file) to slowest (durable taste weights, elsewhere). Layers are meant
to be blended by later work; this module owns the fastest one.

NOW layer (`now_mode_posterior`): a confidence-gated categorical posterior over the user's discovered
taste modes (rec/taste_modes.py), read from the last `now_window_h` hours of REAL plays only.

Why a discrete posterior over modes and not a single blended direction (the way `transient.
centroid_tilt` averages recent vectors into one unit vector)? Averaging raw content vectors across a
short, recent window is exactly the wrong shape for "right now": two or three plays are enough for
someone to hop between unrelated corners of their taste (a house track, then a chill acoustic one),
and blending their vectors produces a "free vector" that points at neither region: a meaningless
midpoint direction nothing in the catalogue actually sounds like. Chasing that phantom average from
moment to moment is whiplash for no benefit. Classifying each recent play to its NEAREST existing
taste mode and reporting the resulting mix as shares keeps every unit of evidence anchored to a real,
previously-discovered region of the user's taste, so "half house, half chill" reads as exactly that
(a split posterior across two real modes) instead of a vector for a genre that doesn't exist.

The confidence gate matters as much as the shape: with only one or two real plays to go on, ANY
posterior is a guess dressed up as a read. `now_min_events` distinct played keys (that have a known
sound) must clear before this returns anything; below that, quiet hours and thin evidence return
None, not a weak posterior a caller might mistake for a strong one.

SESSION layer (`session_tilt`): a unit direction over plays from the last 24 hours of REAL plays,
each wall-clock decayed by `session_halflife_h` (hours scale). Same conceptual sibling as centroid_tilt
but tuned for within-session carry-over. Mirrors centroid_tilt's style: accumulates weighted vectors,
returns a renormalized unit direction or None when quiet/below gate.
"""
import numpy as np

from yt_playlist.rec import embed, rec_params, transient


def now_mode_mix(store, now):
    """Shared internals for `now_mode_posterior`, `now_layer_reading`, and the taste_viz NOW ribbon:
    the {mode_id: share} posterior, `n` (the count of distinct played keys that fed it), and the
    active mode list used to build it (so a caller wanting mode labels doesn't have to re-fetch them).
    Returns (None, 0, modes) below the confidence gate - `modes` may still be non-empty even when the
    gate isn't cleared. Public (not `_`-prefixed): taste_viz needs the full mix, not just the reduced
    readings the other two callers take from it.
    """
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return None, 0, modes
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return None, 0, modes

    window_h = rec_params.get_param(store, "now_window_h")
    min_events = int(rec_params.get_param(store, "now_min_events"))
    since = now - window_h * 3600.0
    rows = store.play_events_since(since)

    # Dedup per key, keeping the latest timestamp in the window (rows arrive oldest-first, so a plain
    # overwrite lands on the latest occurrence of each key).
    latest: dict = {}
    for r in rows:
        latest[r["identity_key"]] = r["played_at"]

    played_keys = [k for k in latest if k in cidx]
    if len(played_keys) < min_events:
        return None, 0, modes

    mode_ids = [m["mode_id"] for m in modes]
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])

    counts: dict = {}
    for k in played_keys:
        v = CV[cidx[k]].astype(np.float64)
        mid = mode_ids[int((C @ v).argmax())]
        counts[mid] = counts.get(mid, 0) + 1

    total = sum(counts.values())
    return {mid: c / total for mid, c in counts.items()}, total, modes


def now_mode_posterior(store, now) -> dict | None:
    """{mode_id: share} over the user's active taste modes, read from the last `now_window_h` hours of
    real plays (`store.play_events_since`), or None below the confidence gate.

    Plays are deduped per identity_key (each key's latest timestamp in the window wins, though the
    timestamp itself doesn't otherwise matter here: this layer classifies WHAT was played, not WHEN).
    Each distinct played key with a content vector is assigned to its nearest active-mode centroid by
    cosine (content vectors and mode centroids are both L2-unit, so cosine == dot; same nearest-
    centroid rule as mode_surfaces._nearest_mode). The posterior is each mode's share of that count,
    so shares sum to 1.0 over modes that received at least one play.

    Returns None when: there are no active taste modes; no content vector model has been built; or
    fewer than `now_min_events` distinct played keys in the window have a content vector. Plays are
    read from play_events ONLY (real timestamps), never the day-granular history_items model: a
    noon-bucket history row happening to fall inside the window carries no real-time information and
    must not count toward "right now".
    """
    posterior, _n, _modes = now_mode_mix(store, now)
    return posterior


def now_layer_reading(store, now) -> dict | None:
    """#88 Task 5: the NOW layer boiled down to one legible reading for the taste page -
    {"top_label": str, "top_share": float, "n": int} for the highest-share mode in
    `now_mode_posterior`'s output, or None when that posterior itself is None (quiet hours, or below
    the confidence gate). `n` is the number of distinct played keys that contributed to the posterior
    (a content-vector hit within the NOW window); `top_label` comes from the active mode list
    (`store.modes.list_modes`), matched by mode_id.
    """
    posterior, n, modes = now_mode_mix(store, now)
    if posterior is None:
        return None
    top_mode_id = max(posterior, key=posterior.get)
    label = next((m["label"] for m in modes if m["mode_id"] == top_mode_id), str(top_mode_id))
    return {"top_label": label, "top_share": posterior[top_mode_id], "n": n}


def session_tilt(store, now, V, idx) -> np.ndarray | None:
    """Unit embedding direction from plays in the current listening session: plays from the last
    24 hours (the kernel does the shaping), each wall-clock decayed by `session_halflife_h` (hours).
    Same transient-model sibling as centroid_tilt: accumulates wall-clock-decayed unit vectors,
    returns a single renormalized unit direction or None when quiet/below gate.

    Plays are deduped per identity_key (each key's latest timestamp in the window wins). Only plays
    whose keys exist in idx (the CALLER'S vector index: scoring passes the collaborative V/idx, the
    same space centroid_tilt tilts) contribute. Returns None when: no recent play is in that index;
    fewer than `now_min_events` contributing keys (the shared confidence gate); or zero-norm
    accumulation. Decay is per-event and internal; callers apply no external freshness factor,
    mirroring centroid_tilt.
    """
    min_events = int(rec_params.get_param(store, "now_min_events"))
    session_hl_h = rec_params.get_param(store, "session_halflife_h")
    since = now - 24 * 3600.0
    rows = store.play_events_since(since)

    # Dedup per key, keeping the latest timestamp in the window (rows arrive oldest-first, so a plain
    # overwrite lands on the latest occurrence of each key).
    latest: dict = {}
    for r in rows:
        latest[r["identity_key"]] = r["played_at"]

    played_keys = [k for k in latest if k in idx]
    if len(played_keys) < min_events:
        return None

    # tilt accumulates a wall-clock-decayed vector sum of UNIT directions across recent plays;
    # each contribution is decayed by decay_weight(age, half_life) (fresher plays count for more)
    # and the whole sum is renormalized to a single unit direction at the end.
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    for k in played_keys:
        ts = latest[k]
        v = V[idx[k]]
        nrm = np.linalg.norm(v)
        if nrm == 0:
            continue
        # session_halflife_h is in hours; decay_weight expects half_life_d in days.
        tilt = tilt + transient.decay_weight(now - ts, session_hl_h / 24.0) * (v / nrm)

    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None  # unit direction, or None when nothing accumulated


def session_mode_mix(store, now):
    """The SESSION layer's legible companion to `session_tilt`. `session_tilt` blends recent plays
    into a single unit direction in the collaborative embedding space - real signal, but not something
    that can be honestly labeled (a blend of two unrelated corners of taste is a "free vector" that
    points at neither, the same reason `now_mode_posterior` classifies rather than averages). This
    function anchors the same session-scale evidence to the user's real, previously-discovered taste
    modes instead, exactly like `now_mode_mix` does for the NOW layer: same content-vector space, same
    nearest-mode-centroid classification, same dedup-latest-per-key, same `now_min_events` confidence
    gate. The only difference is the window and the weighting - a 24-hour window (vs NOW's
    `now_window_h`), with each played key's classification contributing
    `transient.decay_weight(age, session_halflife_h / 24.0)` (fresher plays count for more) instead of
    a flat 1, so the mix reads as "this session's mode balance", decaying smoothly rather than falling
    off a window edge.

    Returns (shares, n, modes): `shares` is {mode_id: share} normalized to sum to 1.0 (weighted by
    decay, not raw count), or None below the gate; `n` is the number of distinct played keys that
    contributed (unweighted, matching `now_mode_mix`'s `n`); `modes` is the active mode list. Below the
    gate this returns None, never a weak guess, mirroring every other layer in this module.
    """
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return None, 0, modes
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return None, 0, modes

    min_events = int(rec_params.get_param(store, "now_min_events"))
    session_hl_h = rec_params.get_param(store, "session_halflife_h")
    since = now - 24 * 3600.0
    rows = store.play_events_since(since)

    # Dedup per key, keeping the latest timestamp in the window (rows arrive oldest-first, so a plain
    # overwrite lands on the latest occurrence of each key).
    latest: dict = {}
    for r in rows:
        latest[r["identity_key"]] = r["played_at"]

    played_keys = [k for k in latest if k in cidx]
    if len(played_keys) < min_events:
        return None, 0, modes

    mode_ids = [m["mode_id"] for m in modes]
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])

    weights: dict = {}
    for k in played_keys:
        v = CV[cidx[k]].astype(np.float64)
        mid = mode_ids[int((C @ v).argmax())]
        weights[mid] = weights.get(mid, 0.0) + transient.decay_weight(now - latest[k], session_hl_h / 24.0)

    total = sum(weights.values())
    n = len(played_keys)
    if total <= 0:
        return None, n, modes
    return {mid: w / total for mid, w in weights.items()}, n, modes
