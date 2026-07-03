"""Graduation: turn sustained transient signals (likes, dislikes, plays, held sliders) into lasting
permanent-weight nudges, gated by the THEME_THRESHOLD ledger. apply_dislikes folds a sync's like/
dislike statuses into the model."""
import math
import time

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
                               source=rec_params.get_param(store, "source_w_dislike"),
                               source_label="dislike")
            if key in existing_like:
                store.clear_like(key)                       # a dislike supersedes a prior like
        elif status == "LIKE":
            if key not in existing_like and store.record_like(key, now):
                graduate_moods(store, [key], 1.0, now,
                               source=rec_params.get_param(store, "source_w_like"),
                               source_label="like")
            if key in existing_dis:
                store.clear_dislike(key)                    # a like clears a prior dislike (preserved)
        elif status == "INDIFFERENT":
            if key in existing_dis:
                store.clear_dislike(key)
            if key in existing_like:
                store.clear_like(key)


def graduate_facet(store, axis, signed, now, source=1.0, source_label="event") -> None:
    """Accumulate one facet's signed event into the graduation ledger; when its running total
    crosses THEME_THRESHOLD, graduate it (a gentle permanent weight nudge, then a smooth reset).
    `source` is the signal's SOURCE_W_* weight (graduation speed). `source_label` names the driving
    signal for the §1c graduation log. Model-only. NEVER suppresses."""
    if not rec_params.get_param(store, "graduation_enabled"):
        return
    threshold = rec_params.get_param(store, "theme_threshold")
    score = store.bump_theme(axis, signed * source, now)
    if abs(score) >= threshold:
        factor = (rec_params.get_param(store, "graduate_up") if score > 0
                  else rec_params.get_param(store, "graduate_down"))
        new_weight = store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                        now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
        store.discount_theme(axis, math.copysign(threshold, score))
        store.log_graduation(axis, source_label, score, factor, new_weight, now)


def graduate_moods(store, keys, signed, now, source=1.0, source_label="mood") -> None:
    """Accumulate a transient-feeding event into the per-facet graduation ledger (presence-weighted),
    graduating each facet that crosses the threshold. `source` is the signal's SOURCE_W_* weight,
    `source_label` names it for the graduation log. Model-only. NEVER suppresses. `signed` carries
    intensity (±1, ±2 on 'a lot')."""
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    n = len(set(keys)) or 1
    for axis, axis_keys in facets.items():
        graduate_facet(store, axis, signed * (len(axis_keys) / n), now,
                       source=source, source_label=source_label)


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
            halflife = rec_params.get_param(store, "weight_revert_halflife_d")
            old_perm = store.get_weights(now=now, revert_halflife_d=halflife).get(axis, 1.0)
            new_perm = store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                          now=now, revert_halflife_d=halflife)
            ratio = (new_perm / old_perm) if old_perm > 0 else 1.0
            store.set_lean(axis, value / ratio, now)          # conserve: new_perm*(value/ratio) == old_perm*value
            store.discount_theme(axis, math.copysign(threshold, score))
            store.log_graduation(axis, "slider", score, factor, new_perm, now)


def graduate_play_exposure(store, now) -> None:
    """Plays graduate by daily EXPOSURE, the same way held sliders do (graduate_slider_exposure).

    Plays feed only the transient model (sync stores history; transient.play_facet_leans reads it).
    Here, once per UTC day per axis, the sustained play-derived lean contributes lean_magnitude *
    SOURCE_W_PLAY to the rec_theme ledger; crossing THEME_THRESHOLD nudges the permanent weight.
    Unlike sliders there is NO migration step: plays are not a displayed handle to keep sticky, and
    the play lean self-decays on the wall clock (#85) when listening stops, so the daily
    contribution falls to ~0 on its own.

    This replaces the old graduate_plays, which wrote permanent weights from a per-session-capped
    event accumulator fed the whole recent-history window every sync. Because the fast plays-sync
    re-fetches the same window each run, that path re-counted and ratcheted genre weights toward
    GENRE_MAX (#46). The per-UTC-day stamp (rec_play_grad) makes re-runs idempotent by construction.
    """
    if not rec_params.get_param(store, "graduation_enabled"):
        return
    threshold = rec_params.get_param(store, "theme_threshold")
    w_play = rec_params.get_param(store, "source_w_play")
    today = _utc_day(now)
    for axis, lean in transient.play_facet_leans(store, now).items():
        if store.get_play_graduated_day(axis) == today:
            continue                                          # already exposed this axis today
        store.set_play_graduated_day(axis, today)             # stamp the day either way
        magnitude = abs(lean)
        if magnitude == 0.0:
            continue
        score = store.bump_theme(axis, magnitude * w_play, now)    # plays only ever push up
        if abs(score) >= threshold:
            factor = (rec_params.get_param(store, "graduate_up") if score > 0
                      else rec_params.get_param(store, "graduate_down"))
            new_weight = store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                            now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
            store.discount_theme(axis, math.copysign(threshold, score))
            store.log_graduation(axis, "play", score, factor, new_weight, now)
