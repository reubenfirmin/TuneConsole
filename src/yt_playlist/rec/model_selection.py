"""Daily-selection presentation and follow-up for saved legacy model selections.

New comparisons are prepared by daily_model_selection. Legacy saved runs retain
their display, restoration, and prospective forecast measurements.
"""
from datetime import datetime, timezone

from yt_playlist.rec import embedding_training as training
from yt_playlist.rec.model_setup import current_setup
from yt_playlist.rec.model_forecasts import forecast, forecast_vectors, followup_intervals, followup_facts, Followup

DAY = 86400
MIN_TRIALS = 50
METHODS = {"svd": "Playlist patterns (SVD)", "item2vec": "Track co-occurrence (Item2Vec)"}


def _tracked_run(store, latest):
    from yt_playlist.library.model_composition import followup
    return followup(store).tracked(latest)


def readiness(store, now):
    from yt_playlist.rec import daily_model_selection
    return daily_model_selection.readiness(store, now)


def _forecast(store, previous, selected, now):
    """Save both top-20 predictions on the same candidate universe, before observing outcomes."""
    from yt_playlist.library.model_composition import forecast as capture_forecast
    return capture_forecast(store, previous, selected, now)


def update_followup(store, now, *, now_fn=None):
    from yt_playlist.library.model_composition import followup
    return followup(store).update(now, now_fn=now_fn)


def maintain(store, now, *, now_fn=None):
    update_followup(store, now, now_fn=now_fn)
    from yt_playlist.rec import daily_model_selection
    return daily_model_selection.maintain(store, now)


def recover_interrupted(store):
    for run in store.model_selection.selection_runs(limit=1):
        if run["status"] == "running":
            if run.get('kind') == 'daily':
                continue  # Detached comparisons resume from their saved predictions.
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
    return {**state, "runs": runs, "last": displayed or latest, "latest": latest, 'daily': True,
            "current": current_setup(store), "min_trials": MIN_TRIALS, "min_windows": 1,
            "min_active_days": 7,
            "restore_id": store.model_selection.active_run_id(),
            "version": f"{latest['id'] if latest else 0}:{latest['status'] if latest else 'waiting'}:"
                       f"{len(latest.get('_evaluations', [])) if latest else 0}:"
                       f"{store.rec_model.rec_vectors_count()}:{current_setup(store)['dim']}:{state['state']}:"
                       + ':'.join(str(check['value']) for check in state['checks'])}
