"""Daily-selection presentation and follow-up for saved legacy model selections.

New comparisons are prepared by daily_model_selection. Legacy saved runs retain
their display, restoration, and prospective forecast measurements.
"""
from datetime import datetime, timezone

from yt_playlist.rec import embedding_training as training
from yt_playlist.rec.model_setup import current_setup
from yt_playlist.rec.vector_math import rank_by_context

DAY = 86400
MIN_TRIALS = 50
METHODS = {"svd": "Playlist patterns (SVD)", "item2vec": "Track co-occurrence (Item2Vec)"}


def _tracked_run(store, latest):
    if latest and latest.get("diagnostic_challenger"):
        return latest
    active = store.settings.get_setting("rec_selection_active_run")
    if active:
        return store.model_selection.selection_run(active)
    restored = store.settings.get_setting("rec_selection_display_run")
    restored = store.model_selection.selection_run(restored) if restored else None
    if restored and (not latest or latest["ran_at"] <= restored.get("restored_at", 0)):
        return restored
    return latest


def readiness(store, now):
    from yt_playlist.rec import daily_model_selection
    return daily_model_selection.readiness(store, now)


def _forecast(store, previous, selected, now):
    """Save both top-20 predictions on the same candidate universe, before observing outcomes."""
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        baskets = reader.rec_evidence.rec_baskets()
        weights = training.cooc_weights(reader.rec_query.play_counts()) if reader.settings.get_setting("rec_cooc_weighting") == "1" else None
        context = reader.history.history_keys_before(now) | reader.model_selection.selection_play_keys(0, now)
    old_keys, old_vectors = training.build_vectors_from_baskets(baskets, dim=previous["dim"], method=previous["method"], weights=weights)
    if previous == selected:
        keys, vectors = old_keys, old_vectors
    else:
        keys, vectors = training.build_vectors_from_baskets(baskets, dim=selected["dim"], method=selected["method"], weights=weights)
    eligible = (set(old_keys) & set(keys)) - context
    if len(eligible) <= 20 or not (context & set(old_keys)) or not (context & set(keys)):
        return None
    return {"eligible": sorted(eligible), "previous": sorted(rank_by_context(old_keys, old_vectors, context, eligible)),
            "selected": sorted(rank_by_context(keys, vectors, context, eligible))}


def update_followup(store, now, *, now_fn=None):
    runs = store.model_selection.selection_runs(limit=1)
    run = _tracked_run(store, runs[0] if runs else None)
    if not run or run["status"] not in ("applied", "retained", "restored"):
        return
    if run.get('kind') in ('product', 'daily'):
        return  # Daily historical tests and old product trials have their own reports.
    if (run.get('followup') or {}).get('frozen_at') is not None:
        return  # Retained summary has an explicit cutoff; source forecasts expired.
    forecasts = store.model_selection.model_forecasts(run["id"])
    end = min(now, run.get("restored_at", now), run.get("ended_at", now))
    total = old_hits = new_hits = excluded = 0
    points = []
    seen = set()
    # A newly imported event can revise an earlier interval, so recompute cheap counts from the
    # persisted forecasts. No fitting or selection-test scores enter these measurements.
    for i, forecast in enumerate(forecasts):
        stop = min(end, forecasts[i + 1]["made_at"]) if i + 1 < len(forecasts) else end
        start = forecast["made_at"]
        if stop <= start:
            continue
        held = (store.model_selection.selection_play_keys(start, stop) & set(forecast["eligible"])) - seen
        # Late imports may establish that a track was already played before the forecast.
        # Revise eligibility instead of calling that repeat a successful new prediction.
        held -= store.history.history_keys_before(start) | store.model_selection.selection_play_keys(0, start)
        generated = store.rec_evidence.generated_only_keys_between(start, stop)
        excluded += len(held & generated)
        held -= generated
        seen |= held
        total += len(held)
        old_hits += len(held & set(forecast["previous"]))
        new_hits += len(held & set(forecast["selected"]))
        if held:
            points.append({"at": stop, "trials": total, "previous_hits": old_hits, "selected_hits": new_hits,
                           "previous_score": old_hits / total, "score": new_hits / total})
    run["followup"] = {"trials": total, "previous_hits": old_hits, "selected_hits": new_hits,
                       "previous_score": old_hits / total if total else None,
                       "score": new_hits / total if total else None, "updated_at": now,
                       "generated_excluded": excluded, "points": points, "forecasts": len(forecasts)}
    store.model_selection.save_selection(run["id"], run)
    if (run["status"] != "restored" and not run.get("ended_at") and store.settings.get_setting("rec_selection_paused") != "1"
            and (not forecasts or now - forecasts[-1]["made_at"] >= DAY)):
        forecast = _forecast(store, run["previous"], run.get("diagnostic_challenger", run["selected"]), now)
        if forecast:
            store.model_selection.save_model_forecast(run["id"], now_fn() if now_fn else now, forecast)
            run["followup"]["forecasts"] = len(forecasts) + 1
            store.model_selection.save_selection(run["id"], run)


def maintain(store, now, *, now_fn=None):
    update_followup(store, now, now_fn=now_fn)
    from yt_playlist.rec import daily_model_selection
    return daily_model_selection.maintain(store, now)


def recover_interrupted(store):
    for run in store.model_selection.selection_runs(limit=1):
        if run["status"] == "running":
            if run.get('kind') == 'daily':
                continue  # Detached comparisons resume from their saved predictions.
            if run.get('kind') == 'product':
                # Observing is durable work, not an interrupted fit. Preparation
                # is also safe to retry because it never replaces live vectors.
                from yt_playlist.rec import product_comparison
                if product_comparison.state(store).get('run_id') == run['id']:
                    continue
            run.update(status="failed", reason="The app stopped during the comparison. Your current model was kept.")
            store.model_selection.save_selection(run["id"], run)


def view(store, now, *, limit=6):
    state = readiness(store, now)
    runs = store.model_selection.selection_runs(limit=limit)
    latest = runs[0] if runs else None
    tracked = _tracked_run(store, latest)
    display_id = str(tracked["id"]) if tracked else None
    displayed = next((r for r in runs if str(r["id"]) == display_id), None)
    if display_id and not displayed:
        displayed = store.model_selection.selection_run(display_id)
        if displayed:
            runs.append(displayed)
    def date(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d, %Y")
    for run in runs:
        run["date"] = date(run["ran_at"])
        if run.get("restored_at"):
            run["restored_date"] = date(run["restored_at"])
        for key in ("previous", "selected", *(['challenger'] if run.get('challenger') else [])):
            setup = run[key]
            setup["label"] = f"{METHODS.get(setup['method'], setup['method'])}, {setup['dim']} features"
        followup = run.get("followup", {})
        if followup.get("updated_at"):
            followup["updated_date"] = date(followup["updated_at"])
        for candidate in run.get("grid", []):
            candidate["label"] = METHODS.get(candidate["method"], candidate["method"])
        followup["enough"] = followup.get("trials", 0) >= MIN_TRIALS
        # Both lines share a fixed 0–100% scale. Preserve true zero rather than hiding it.
        for key, score_key in (("previous_path", "previous_score"), ("selected_path", "score")):
            points = followup.get("points", [])
            for point in points:
                point["date"] = date(point["at"])
            start = run["ran_at"]
            end = max(start + 1, followup.get("updated_at", start))
            followup[key] = " ".join(f"{40 + 620 * (p['at'] - start) / (end - start):.2f},{160 - 140 * p[score_key]:.2f}" for p in points)
        run["followup"] = followup
    if state.get("next_at"):
        state["next_date"] = date(state["next_at"])
    return {**state, "runs": runs, "last": displayed or latest, "latest": latest, 'product': {}, 'daily': True,
            "current": current_setup(store), "min_trials": MIN_TRIALS, "min_windows": 1,
            "min_active_days": 7,
            "restore_id": store.settings.get_setting("rec_selection_active_run"),
            "version": f"{latest['id'] if latest else 0}:{latest['status'] if latest else 'waiting'}:"
                       f"{len(latest.get('_evaluations', [])) if latest else 0}:"
                       f"{store.rec_model.rec_vectors_count()}:{current_setup(store)['dim']}:{state['state']}:"
                       + ':'.join(str(check['value']) for check in state['checks'])}
