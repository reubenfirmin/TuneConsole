"""Automatic, paired model selection and genuinely prospective measurement.

Candidate fits never touch the serving vectors. Selection uses the same historical targets for
every candidate, requires improvement in multiple windows, and corrects its paired sign test for
the number of alternatives. Daily shadow forecasts are saved BEFORE their outcomes exist; later
syncs score both setups against the same eligible, independently played tracks.
"""
import math
import json
from datetime import datetime, timezone

import numpy as np

from yt_playlist.rec import embedding_training as training, eval_recs

DAY = 86400
MIN_HISTORY_DAYS = 14
MIN_TRACKS = 100
MIN_TRIALS = 50
MIN_WINDOWS = 2
RECHECK_DAYS = 28
MIN_FRESH_TRACKS = 30
CONFIGS = [("svd", 48), ("svd", 64), ("svd", 96), ("svd", 128), ("item2vec", 64)]
METHODS = {"svd": "Playlist patterns (SVD)", "item2vec": "Track co-occurrence (Item2Vec)"}


def current_setup(store):
    return {"method": store.settings.get_setting("rec_embed_method") or "svd",
            "dim": int(store.settings.get_setting("rec_dim") or training.DIM)}


def _tracked_run(store, latest):
    active = store.settings.get_setting("rec_selection_active_run")
    if active:
        return store.model_selection.selection_run(active)
    restored = store.settings.get_setting("rec_selection_display_run")
    restored = store.model_selection.selection_run(restored) if restored else None
    if restored and (not latest or latest["ran_at"] <= restored.get("restored_at", 0)):
        return restored
    return latest


def readiness(store, now):
    runs = store.model_selection.selection_runs(limit=1)
    last = runs[0] if runs else None
    lo, hi = store.charts.history_bounds()
    days = max(0, (min(hi, now) - lo) / DAY) if lo is not None and hi is not None else 0
    tracks = store.rec_model.rec_vectors_count()
    checks = [{"label": "Listening history", "value": min(int(days), MIN_HISTORY_DAYS), "total": MIN_HISTORY_DAYS,
               "unit": "days"},
              {"label": "Modeled tracks", "value": min(tracks, MIN_TRACKS), "total": MIN_TRACKS, "unit": "tracks"}]
    if last and last["status"] == "running":
        return {"state": "running", "reason": last.get("progress", "Comparing model setups."), "checks": checks}
    if store.settings.get_setting("rec_selection_restore"):
        return {"state": "restoring", "reason": "Restoring the previous model after the current update finishes.", "checks": checks}
    if store.settings.get_setting("rec_selection_paused") == "1":
        return {"state": "paused", "reason": "Automatic selection is paused after restoring your previous model.", "checks": checks}
    if days < MIN_HISTORY_DAYS or tracks < MIN_TRACKS:
        return {"state": "waiting", "reason": "Another comparison needs at least 14 days of history and 100 modeled tracks." if last else "Waiting for enough listening history and modeled tracks to make a useful comparison.",
                "checks": checks}
    if last:
        retry_days = {"failed": 1, "insufficient": 7}.get(last["status"], RECHECK_DAYS)
        tracked = _tracked_run(store, last)
        restored_at = tracked.get("restored_at", 0) if tracked else 0
        next_at = max(last["ran_at"], restored_at) + retry_days * DAY
        if now < next_at:
            return {"state": "waiting", "reason": "Waiting before retrying the interrupted comparison." if last["status"] == "failed" else "Collecting listening evidence before the next comparison.",
                    "next_at": next_at, "checks": checks}
        if last["status"] == "failed":
            return {"state": "ready", "reason": "Ready to retry automatically on the next model update.", "checks": checks}
        since = max(last.get("evidence_end", last["ran_at"]), restored_at)
        fresh = len(store.history.history_keys_between(since, now) - store.rec_evidence.generated_only_keys_between(since, now))
        checks.append({"label": "Fresh listening since the last comparison", "value": min(fresh, MIN_FRESH_TRACKS),
                       "total": MIN_FRESH_TRACKS, "unit": "distinct tracks"})
        if fresh < MIN_FRESH_TRACKS:
            return {"state": "waiting", "reason": "Waiting for fresh listening evidence; repeating the same test would not tell us more.",
                    "checks": checks}
    return {"state": "ready", "reason": "Ready to compare automatically on the next model update.", "checks": checks}


def _windows(store, now):
    """Freeze the input data once, before any candidate is fitted."""
    _, hi = store.charts.history_bounds()
    hi = min(hi, now)
    span = eval_recs.best_holdout(store) * DAY
    weighted = store.settings.get_setting("rec_cooc_weighting") == "1"
    out = []
    for i in range(3):
        end = hi - i * span + 1e-6
        cutoff = end - span
        baskets, _ = store.rec_evidence.rec_baskets_as_of(cutoff)
        before = store.history.history_keys_before(cutoff)
        held = store.history.history_keys_between(cutoff, end) - before
        generated = store.rec_evidence.generated_only_keys_between(cutoff, end)
        weights = {k: 1 + math.log1p(v) for k, v in store.history.play_counts_before(cutoff).items()} if weighted else None
        out.append({"baskets": baskets, "before": before, "held": held - generated,
                    "cutoff": cutoff, "end": end, "weights": weights})
    return out, hi


def _rank(keys, vectors, context, eligible, k=20):
    idx = {key: i for i, key in enumerate(keys)}
    context = sorted(context & set(idx))
    if not context or not eligible:
        return set()
    centroid = vectors[[idx[key] for key in context]].mean(0)
    norm = np.linalg.norm(centroid)
    if norm < 1e-9:
        return set()
    scores = vectors @ (centroid / norm)
    # Stable ties make a retained setup produce exactly the same shadow forecast.
    return set(sorted(eligible, key=lambda key: (-float(scores[idx[key]]), key))[:k])


def _evaluate(setup, windows):
    outcomes = []
    for window in windows:
        keys, vectors = training.build_vectors_from_baskets(window["baskets"], dim=setup["dim"],
                                                        method=setup["method"], weights=window["weights"])
        eligible = set(keys) - window["before"]
        held = window["held"] & eligible
        if not held or not (set(keys) & window["before"]) or len(eligible) <= 20:
            outcomes.append({})
            continue
        ranked = _rank(keys, vectors, window["before"], eligible)
        outcomes.append({key: int(key in ranked) for key in held})
    return outcomes


def _paired(previous, candidate, alternatives):
    """Paired outcomes, stable gains across windows, and a family-wise 5% sign-test gate."""
    n = old_hits = new_hits = wins = losses = 0
    windows = []
    for old, new in zip(previous, candidate):
        common = old.keys() & new.keys()
        if old and not common:
            return {"eligible": False, "reason": "Incomplete test coverage", "trials": 0}
        if not common:
            continue
        # A candidate cannot win by dropping harder-to-predict baseline targets.
        if common != old.keys():
            return {"eligible": False, "reason": "Incomplete test coverage", "trials": 0}
        oh, nh = sum(old[k] for k in common), sum(new[k] for k in common)
        windows.append({"trials": len(common), "previous_hits": oh, "selected_hits": nh})
        n += len(common)
        old_hits += oh
        new_hits += nh
        wins += sum(new[k] > old[k] for k in common)
        losses += sum(new[k] < old[k] for k in common)
    discordant = wins + losses
    p = math.fsum(math.exp(math.lgamma(discordant + 1) - math.lgamma(j + 1)
                         - math.lgamma(discordant - j + 1) - discordant * math.log(2))
                  for j in range(wins, discordant + 1)) if discordant else 1.0
    enough = n >= MIN_TRIALS and len(windows) >= MIN_WINDOWS
    stable = sum(w["selected_hits"] > w["previous_hits"] for w in windows) >= MIN_WINDOWS
    stable = stable and all(w["selected_hits"] >= w["previous_hits"] for w in windows)
    promote = enough and stable and new_hits - old_hits >= 5 and (new_hits - old_hits) / n >= .02 and p <= .05 / max(1, alternatives)
    return {"eligible": enough, "promote": promote, "trials": n, "windows": len(windows),
            "previous_hits": old_hits, "selected_hits": new_hits,
            "previous_score": old_hits / n if n else None, "score": new_hits / n if n else None,
            "p": p, "reason": "Clear improvement" if promote else "No reliable improvement" if enough else "Not enough comparable test tracks"}


def _build(store, setup):
    return training.build_vectors_from_baskets(
        store.rec_evidence.rec_baskets(), dim=setup["dim"], method=setup["method"],
        weights=training.cooc_weights(store.rec_query.play_counts()) if store.settings.get_setting("rec_cooc_weighting") == "1" else None)


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
    return {"eligible": sorted(eligible), "previous": sorted(_rank(old_keys, old_vectors, context, eligible)),
            "selected": sorted(_rank(keys, vectors, context, eligible))}


def update_followup(store, now, *, now_fn=None):
    runs = store.model_selection.selection_runs(limit=1)
    run = _tracked_run(store, runs[0] if runs else None)
    if not run or run["status"] not in ("applied", "retained", "restored"):
        return
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
        forecast = _forecast(store, run["previous"], run["selected"], now)
        if forecast:
            store.model_selection.save_model_forecast(run["id"], now_fn() if now_fn else now, forecast)
            run["followup"]["forecasts"] = len(forecasts) + 1
            store.model_selection.save_selection(run["id"], run)


def run(store, now, *, now_fn=None):
    previous = current_setup(store)
    result = {"ran_at": now, "status": "running", "previous": previous, "selected": previous,
              "progress": "Preparing historical listening tests.", "grid": []}
    run_id = store.model_selection.start_selection(now, result)
    completed = False
    try:
        with store.read_snapshot() as snapshot:
            windows, result["evidence_end"] = _windows(snapshot.store, now)
        baseline = _evaluate(previous, windows)
        candidates = [dict(method=method, dim=dim) for method, dim in CONFIGS if (method, dim) != (previous["method"], previous["dim"])]
        baseline_check = _paired(baseline, baseline, len(candidates))
        result["baseline"] = baseline_check
        if not baseline_check["eligible"]:
            result.update(status="insufficient", reason="Not enough reconstructable listening tests: need 50 comparable tracks across at least two periods.")
        else:
            for i, setup in enumerate(candidates):
                result["progress"] = f"Comparing setup {i + 1} of {len(candidates)}. Your current model stays in use."
                store.model_selection.save_selection(run_id, result)
                try:
                    measured = _paired(baseline, _evaluate(setup, windows), len(candidates))
                    result["grid"].append({**setup, **measured})
                except (ValueError, np.linalg.LinAlgError):
                    result["grid"].append({**setup, "eligible": False, "reason": "Could not fit this setup"})
            contenders = [g for g in result["grid"] if g.get("promote")]
            best = max(contenders, key=lambda g: g["score"]) if contenders else None
            if best and store.settings.get_setting("rec_selection_paused") != "1":
                selected = {k: best[k] for k in ("method", "dim")}
                # Fit in memory, then publish exactly once. A failed fit leaves the live model alone.
                with store.read_snapshot() as snapshot:
                    keys, vectors = _build(snapshot.store, selected)
                    if not keys:
                        raise ValueError("Selected setup could not build a serving model")
                    published = {**result, "status": "applied", "selected": selected, "comparison": best,
                                 "reason": "Improved predictions across multiple test periods."}
                    rows = [(k, vectors[i].tobytes()) for i, k in enumerate(keys)]
                    encoded = json.dumps(published)
                    if snapshot.publish(lambda: store.model_selection.publish_selection(
                            run_id, encoded, rows, selected)):
                        result = published
                    else:
                        result.update(status="failed", reason="Inputs changed during fitting. Your current setup was kept.")
            else:
                result.update(status="retained", comparison=baseline_check,
                              reason="No alternative showed a reliable improvement. Your current setup was kept.")
        store.model_selection.save_selection(run_id, result)
        completed = True
        update_followup(store, now_fn() if now_fn else now, now_fn=now_fn)
    except Exception:
        # If publication succeeded, never misreport that the old model is still live merely
        # because the best-effort prospective forecast failed afterward.
        if not completed:
            result.update(status="failed", reason="The comparison could not finish. Your current model was kept.")
            store.model_selection.save_selection(run_id, result)
        raise
    return result


def maintain(store, now, *, now_fn=None):
    update_followup(store, now, now_fn=now_fn)
    if readiness(store, now)["state"] == "ready":
        return run(store, now, now_fn=now_fn)


def recover_interrupted(store):
    for run in store.model_selection.selection_runs(limit=1):
        if run["status"] == "running":
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
        for key in ("previous", "selected"):
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
    return {**state, "runs": runs, "last": displayed or latest, "latest": latest,
            "current": current_setup(store), "min_trials": MIN_TRIALS, "min_windows": MIN_WINDOWS,
            "restore_id": store.settings.get_setting("rec_selection_active_run"),
            "version": f"{latest['id'] if latest else 0}:{latest['status'] if latest else 'waiting'}:{store.rec_model.rec_vectors_count()}:{current_setup(store)['dim']}:{state['state']}"}
