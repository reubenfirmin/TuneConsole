"""Graduation: turn sustained transient signals (likes, dislikes, plays, held sliders) into lasting
permanent-weight nudges, gated by the THEME_THRESHOLD ledger. apply_dislikes folds a sync's like/
dislike statuses into the model."""
import math
import time
from collections import Counter

from yt_playlist.rec import rec_params, transient


def apply_dislikes(store, status_map, now) -> None:
    """Fold a sync's per-track likeStatus into the model. A first-seen DISLIKE -> a long global
    suppression + a negative graduation contribution. A first-seen LIKE -> a positive transient
    signal (recency-captured) + a positive graduation contribution. A no-longer-disliked/liked track
    has its suppression/like cleared. NO direct permanent axis nudge. Graduation owns that.
    Idempotent."""
    existing_dis = store.disliked_identity_keys()
    existing_like = set(store.recent_liked_keys())
    until = now + rec_params.get_param(store, "dislike_suppress_days") * 86400
    for key, status in status_map.items():
        if status == "DISLIKE":
            if key not in existing_dis and store.record_dislike(key, until, now):
                graduate_moods(store, [key], -1.0, now,
                               source=rec_params.get_param(store, "source_w_dislike"))
            if key in existing_like:
                store.clear_like(key)                       # a dislike supersedes a prior like
        elif status == "LIKE":
            if key not in existing_like and store.record_like(key, now):
                graduate_moods(store, [key], 1.0, now,
                               source=rec_params.get_param(store, "source_w_like"))
            if key in existing_dis:
                store.clear_dislike(key)                    # a like clears a prior dislike (preserved)
        elif status == "INDIFFERENT":
            if key in existing_dis:
                store.clear_dislike(key)
            if key in existing_like:
                store.clear_like(key)


def graduate_facet(store, axis, signed, now, source=1.0) -> None:
    """Accumulate one facet's signed event into the graduation ledger; when its running total
    crosses THEME_THRESHOLD, graduate it (a gentle permanent weight nudge, then a smooth reset).
    `source` is the signal's SOURCE_W_* weight (graduation speed). Model-only. NEVER suppresses."""
    if not rec_params.get_param(store, "graduation_enabled"):
        return
    threshold = rec_params.get_param(store, "theme_threshold")
    score = store.bump_theme(axis, signed * source, now)
    if abs(score) >= threshold:
        factor = (rec_params.get_param(store, "graduate_up") if score > 0
                  else rec_params.get_param(store, "graduate_down"))
        store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
        store.discount_theme(axis, math.copysign(threshold, score))


def graduate_moods(store, keys, signed, now, source=1.0) -> None:
    """Accumulate a transient-feeding event into the per-facet graduation ledger (presence-weighted),
    graduating each facet that crosses the threshold. `source` is the signal's SOURCE_W_* weight.
    Model-only. NEVER suppresses. `signed` carries intensity (±1, ±2 on 'a lot')."""
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    n = len(set(keys)) or 1
    for axis, axis_keys in facets.items():
        graduate_facet(store, axis, signed * (len(axis_keys) / n), now, source=source)


def graduate_plays(store, keys, now) -> None:
    """Graduate just-played keys: weak per-play contribution (SOURCE_W_PLAY), with the whole session's
    play contribution capped at PLAY_GRAD_SESSION_CAP so a single binge cannot rewrite taste. Spreads
    the capped budget across the played facets proportionally to presence (counts duplicate plays)."""
    if not keys:
        return
    # Build axis -> play count from the original (possibly duplicate) keys list
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    # Count how many plays each key appears (duplicates count as separate plays)
    play_counts = Counter(keys)
    n = len(keys) or 1                                              # total plays this session
    w_play = rec_params.get_param(store, "source_w_play")
    session_cap = rec_params.get_param(store, "play_grad_session_cap")
    raw = w_play * n                                                # total play intensity this session
    scale = min(1.0, session_cap / raw) if raw > 0 else 0.0
    for axis, axis_keys in facets.items():
        # Sum play counts across all unique keys mapped to this axis
        axis_play_count = sum(play_counts[k] for k in axis_keys)
        contribution = w_play * axis_play_count * scale
        # source already folded into `contribution`; pass source=1.0, signed=+contribution
        graduate_facet(store, axis, contribution, now, source=1.0)


def _utc_day(now) -> str:
    """UTC date string YYYY-MM-DD for a unix timestamp (held-day bucketing; deterministic for tests)."""
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def graduate_slider_exposure(store, now) -> None:
    """Once per distinct held-day per axis, a held standing lean adds lean_magnitude * SOURCE_W_SLIDER
    to its graduation ledger. On crossing THEME_THRESHOLD: a permanent nudge_weight step, then migrate
    by dividing the lean by the actual permanent ratio so the displayed effective multiplier
    (permanent x lean) is conserved (sticky). Returning a slider to neutral (magnitude 0) stops all
    accrual."""
    if not rec_params.get_param(store, "graduation_enabled"):
        return
    threshold = rec_params.get_param(store, "theme_threshold")
    w_slider = rec_params.get_param(store, "source_w_slider")
    today = _utc_day(now)
    for row in store.lean_rows():
        axis, value, last_day = row["axis"], row["value"], row["last_graduated_day"]
        if last_day == today:
            continue                                          # already exposed today
        magnitude = abs(value - 1.0)
        store.set_lean_graduated_day(axis, today)             # stamp the held-day either way
        if magnitude == 0.0:
            continue
        signed = math.copysign(magnitude * w_slider, value - 1.0)
        score = store.bump_theme(axis, signed, now)
        if abs(score) >= threshold:
            factor = (rec_params.get_param(store, "graduate_up") if score > 0
                      else rec_params.get_param(store, "graduate_down"))
            old_perm = store.get_weights().get(axis, 1.0)
            new_perm = store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
            ratio = (new_perm / old_perm) if old_perm > 0 else 1.0
            store.set_lean(axis, value / ratio, now)          # conserve: new_perm*(value/ratio) == old_perm*value
            store.discount_theme(axis, math.copysign(threshold, score))
