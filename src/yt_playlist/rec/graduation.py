"""Project recording ratings and graduate sustained positive signals and held sliders.

Permanent-weight nudges are gated by the registered theme_threshold.
"""
from yt_playlist.rec import mix_tuning, rec_params, scoring, transient
from yt_playlist.rec.graduation_rules import graduation_factor, mood_contributions, migrated_lean, utc_day as _utc_day


def feedback_axis(store, explicit_axis, reason, item):
    """The single taste axis a suggestion-dismiss / why-chip should steer, derived from the
    dismissed track (#43). An explicit axis wins (the Home why-chips, and the 'not this artist'
    tile, which sends artist:<name>). Otherwise map the reason via the track's own metadata.
    Returns None when there is nothing to steer (the track has no genre/decade/artist, or it is
    not actually mainstream), so the caller no-ops gracefully rather than inventing a steer."""
    if explicit_axis and explicit_axis.split(":", 1)[0] in ("genre", "era", "artist", "pop"):
        return explicit_axis
    if reason not in ("vibe", "era", "artist", "mainstream"):
        return None
    # Metadata and its canonical facet must come from one owned read. A
    # synchronized metadata method alone does not protect the later raw genre
    # lookup from borrowing another thread's uncommitted serving transaction.
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return feedback_axis(reader, explicit_axis, reason, item)
    dao = store.rec_query
    if reason == "vibe":
        g = dao.track_genres([item]).get(item)
        return f"genre:{store.genres.feature_axes([g])[g][0]}" if g else None
    if reason == "era":
        d = dao.track_decades([item]).get(item)
        return f"era:{d}" if d else None
    if reason == "artist":
        a = dao.track_artists([item]).get(item)
        return f"artist:{a}" if a else None
    if reason == "mainstream":
        pop = dao.track_popularity([item]).get(item)
        band = scoring._pop_band(pop, rec_params.get_param(store, "pop_mainstream_min"))
        return f"pop:{band}" if band else None
    return None


def mood_tilt(store, keys, signed, now):
    """Record bounded, track-scoped mood feedback."""
    if not keys:
        return
    halflife_d = rec_params.get_param(store, "mood_halflife_d")
    prune_before = now - 4 * halflife_d * 86400
    store.rec_signals.record_mood(keys, signed, now, prune_before=prune_before)


def apply_ratings(store, states):
    """Project combined recording evidence at its original observation time.

    Ratings carry their own observation time and sync/action provenance. Dislikes
    exclude the recording for the configured duration; neutral ratings clear the
    projected state. Callers reconcile confirmed observations before projection.
    """
    store.rec_model.apply_rating_feedback(
        states, dislike_seconds=rec_params.get_param(store, "dislike_suppress_days") * 86400)


def graduate_facet(store, axis, signed, now, source=1.0, source_label="event") -> None:
    """Accumulate one facet's signed event into the graduation ledger; when its running total
    crosses theme_threshold, graduate it (a gentle permanent weight nudge, then a smooth reset).
    `source` is the signal's registered source weight (graduation speed). `source_label` names the driving
    signal for the §1c graduation log. Model-only. NEVER suppresses."""
    from yt_playlist.controllers.graduation import Graduation
    Graduation(store.rec_signals, store.rec_model,
        parameter=lambda name: rec_params.get_param(store, name)).facet(
            axis, signed, now, source=source, source_label=source_label)


def graduate_moods(store, keys, signed, now, source=1.0, source_label="mood") -> None:
    """Accumulate a transient-feeding event into the per-facet graduation ledger (presence-weighted),
    graduating each facet that crosses the threshold. `source` is the signal's registered source weight,
    `source_label` names it for the graduation log. Model-only. NEVER suppresses. `signed` carries
    intensity (±1, ±2 on 'a lot').

    Negative track feedback does not establish a permanent artist, genre, or era preference.
    Short-lived mood choices stay in transient.py. Deliberate facet steering
    still graduates through graduate_facet/slider exposure.
    Positive track signals continue to graduate all their facets."""
    if signed <= 0:
        return
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    for axis, contribution in mood_contributions(keys, facets, signed).items():
        graduate_facet(store, axis, contribution, now,
                       source=source, source_label=source_label)


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
    from yt_playlist.controllers.graduation import Graduation
    Graduation(store.rec_signals, store.rec_model,
        parameter=lambda name: rec_params.get_param(store, name)).slider_exposure(store.rec_model.lean_rows(), now)


def graduate_play_exposure(store, now, *, prepared_leans=None) -> None:
    """Plays graduate by daily EXPOSURE, the same way held sliders do (graduate_slider_exposure).

    Plays feed only the transient model (sync stores history; transient.play_facet_leans reads it).
    Here, once per UTC day per axis, the sustained play-derived lean contributes lean_magnitude *
    source_w_play to the axis contribution; crossing theme_threshold nudges the permanent weight.
    Unlike sliders there is NO migration step: plays are not a displayed handle to keep sticky, and
    the play lean self-decays on the wall clock (#85) when listening stops, so the daily
    contribution falls to ~0 on its own.

    This replaces the old graduate_plays, which wrote permanent weights from a per-session-capped
    event accumulator fed the whole recent-history window every sync. Because the fast plays-sync
    re-fetches the same window each run, that path re-counted and ratcheted genre weights toward
    GENRE_MAX (#46). The per-UTC-day stamp (rec_play_grad) makes re-runs idempotent by construction.
    """
    leans = transient.play_facet_leans(store, now) if prepared_leans is None else prepared_leans
    from yt_playlist.controllers.graduation import Graduation
    Graduation(store.rec_signals, store.rec_model,
        parameter=lambda name: rec_params.get_param(store, name)).play_exposure(leans, now)
