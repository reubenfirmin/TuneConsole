"""Project recording ratings and graduate sustained positive signals and held sliders.

Permanent-weight nudges are gated by the registered theme_threshold.
"""
import math
import time

from yt_playlist.rec import mix_tuning, rec_params, transient


def apply_ratings(store, states):
    """Project combined recording evidence, graduating a positive contribution at most once.

    Ratings carry their own observation time and sync/action provenance. Dislikes
    retain suppression and durable negative evidence; neutral ratings clear the
    projected state. Callers reconcile confirmed observations before projection.
    """
    with store._lock:
        new_likes = store.rec_model.apply_rating_feedback(
            states, dislike_seconds=rec_params.get_param(store, "dislike_suppress_days") * 86400)
        for key, rating in new_likes.items():
            if store.rec_model.mark_graduated_once(key, "like_grad", rating.observed_at):
                w_param = ("source_w_like" if rating.provenance == "action"
                           else "source_w_like_synced")
                graduate_moods(store, [key], 1.0, rating.observed_at,
                               source=rec_params.get_param(store, w_param), source_label="like")


def graduate_facet(store, axis, signed, now, source=1.0, source_label="event") -> None:
    """Accumulate one facet's signed event into the graduation ledger; when its running total
    crosses theme_threshold, graduate it (a gentle permanent weight nudge, then a smooth reset).
    `source` is the signal's registered source weight (graduation speed). `source_label` names the driving
    signal for the §1c graduation log. Model-only. NEVER suppresses."""
    threshold = rec_params.get_param(store, "theme_threshold")
    score = store.rec_signals.bump_theme(axis, signed * source, now)
    if abs(score) >= threshold:
        factor = (rec_params.get_param(store, "graduate_up") if score > 0
                  else rec_params.get_param(store, "graduate_down"))
        new_weight = store.rec_model.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                        now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
        store.rec_signals.discount_theme(axis, math.copysign(threshold, score))
        store.rec_signals.log_graduation(axis, source_label, score, factor, new_weight, now)


def graduate_moods(store, keys, signed, now, source=1.0, source_label="mood") -> None:
    """Accumulate a transient-feeding event into the per-facet graduation ledger (presence-weighted),
    graduating each facet that crosses the threshold. `source` is the signal's registered source weight,
    `source_label` names it for the graduation log. Model-only. NEVER suppresses. `signed` carries
    intensity (±1, ±2 on 'a lot').

    Negative track feedback does not establish a permanent artist, genre, or era preference.
    Its short-lived facets stay in transient.py; explicit dislikes also retain their own vector
    direction. Deliberate facet steering still graduates through graduate_facet/slider exposure.
    Positive track signals continue to graduate all their facets."""
    if signed <= 0:
        return
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
    """Once per distinct held-day per axis, a held standing lean adds lean_magnitude * source_w_slider
    to its graduation ledger. On crossing theme_threshold: a permanent nudge_weight step, then migrate
    by dividing the lean by the actual permanent ratio so the displayed effective multiplier
    (permanent x lean) is conserved (sticky). Returning a slider to neutral (magnitude 0) stops all
    accrual."""
    mix_tuning.expire(store, now)
    # A temporary Home adjustment must not turn into a lasting preference at midnight.
    if mix_tuning.remaining(store, now) > 0:
        return
    threshold = rec_params.get_param(store, "theme_threshold")
    w_slider = rec_params.get_param(store, "source_w_slider")
    today = _utc_day(now)
    for row in store.rec_model.lean_rows():
        axis, value, last_day = row["axis"], row["value"], row["last_graduated_day"]
        if last_day == today:
            continue                                          # already exposed today
        magnitude = abs(value - 1.0)
        store.rec_model.set_lean_graduated_day(axis, today)             # stamp the held-day either way
        if magnitude == 0.0:
            continue
        signed = math.copysign(magnitude * w_slider, value - 1.0)
        score = store.rec_signals.bump_theme(axis, signed, now)
        if abs(score) >= threshold:
            factor = (rec_params.get_param(store, "graduate_up") if score > 0
                      else rec_params.get_param(store, "graduate_down"))
            halflife = rec_params.get_param(store, "weight_revert_halflife_d")
            old_perm = store.rec_model.get_weights(now=now, revert_halflife_d=halflife).get(axis, 1.0)
            new_perm = store.rec_model.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                          now=now, revert_halflife_d=halflife)
            ratio = (new_perm / old_perm) if old_perm > 0 else 1.0
            store.rec_model.set_lean(axis, value / ratio, now)          # conserve: new_perm*(value/ratio) == old_perm*value
            store.rec_signals.discount_theme(axis, math.copysign(threshold, score))
            store.rec_signals.log_graduation(axis, "slider", score, factor, new_perm, now)


def graduate_play_exposure(store, now, *, prepared_leans=None) -> None:
    """Plays graduate by daily EXPOSURE, the same way held sliders do (graduate_slider_exposure).

    Plays feed only the transient model (sync stores history; transient.play_facet_leans reads it).
    Here, once per UTC day per axis, the sustained play-derived lean contributes lean_magnitude *
    source_w_play to the rec_theme ledger; crossing theme_threshold nudges the permanent weight.
    Unlike sliders there is NO migration step: plays are not a displayed handle to keep sticky, and
    the play lean self-decays on the wall clock (#85) when listening stops, so the daily
    contribution falls to ~0 on its own.

    This replaces the old graduate_plays, which wrote permanent weights from a per-session-capped
    event accumulator fed the whole recent-history window every sync. Because the fast plays-sync
    re-fetches the same window each run, that path re-counted and ratcheted genre weights toward
    GENRE_MAX (#46). The per-UTC-day stamp (rec_play_grad) makes re-runs idempotent by construction.
    """
    threshold = rec_params.get_param(store, "theme_threshold")
    w_play = rec_params.get_param(store, "source_w_play")
    today = _utc_day(now)
    leans = transient.play_facet_leans(store, now) if prepared_leans is None else prepared_leans
    for axis, lean in leans.items():
        if store.rec_signals.get_play_graduated_day(axis) == today:
            continue                                          # already exposed this axis today
        store.rec_signals.set_play_graduated_day(axis, today)             # stamp the day either way
        magnitude = abs(lean)
        if magnitude == 0.0:
            continue
        score = store.rec_signals.bump_theme(axis, magnitude * w_play, now)    # plays only ever push up
        if abs(score) >= threshold:
            factor = (rec_params.get_param(store, "graduate_up") if score > 0
                      else rec_params.get_param(store, "graduate_down"))
            new_weight = store.rec_model.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX,
                                            now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
            store.rec_signals.discount_theme(axis, math.copysign(threshold, score))
            store.rec_signals.log_graduation(axis, "play", score, factor, new_weight, now)
