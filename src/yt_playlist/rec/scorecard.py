"""Interpret detached serving history without database work in the calculations."""
import json
import math
import statistics

from yt_playlist.library.listen_derive import classify_exit

MIN_EXPERIMENT_CLUSTERS = 100


def recommendation_scorecard(store, start, end, outcome_window_h=24, min_impressions=100,
                             impression_id=None, experiments_only=False):
    inputs = store.rec_ledger.scorecard_inputs(
        start, end, outcome_window_h, impression_id, experiments_only)
    return summarize(inputs, start, end, outcome_window_h, min_impressions)


def served_recommendation(store, impression_id):
    loaded = store.rec_ledger.served_recommendation_inputs(impression_id)
    if loaded is None:
        return None
    result, inputs = loaded
    result["outcomes"] = (summarize(inputs, result["served_at"], result["served_at"] + 1)
                          if inputs is not None else None)
    return result


def experiment_report(store, start, end, now):
    mature_end = min(end, now - 24 * 3600)
    inputs, shadows = store.rec_ledger.experiment_inputs(start, end, max(start, mature_end))
    report = summarize(inputs, start, max(start, mature_end))
    provenance = {}
    # Inspect the exact mature live rows summarized above. Old cohorts outside
    # the report, pending offers and shadows cannot support or veto a decision.
    for row in inputs[0]:
        trace = json.loads(row['provenance'])
        experiment = trace.get('experiment') or {}
        label = experiment.get('cohort', 'unknown') + ':' + trace.get('ranker', 'unknown')
        evidence = provenance.setdefault(label, {'controlled': 0, 'uncontrolled': 0, 'legacy_cache': 0})
        probability = trace.get('propensity')
        controlled = (experiment.get('ranker_controlled') is True
                      and type(probability) in (int, float) and math.isfinite(probability)
                      and 0 < probability < 1)
        evidence['controlled' if controlled else 'uncontrolled'] += 1
        evidence['legacy_cache'] += int((experiment.get('ppr_policy') or {}).get('algorithm')
                                        == 'unknown-legacy-cache')
    for label, bucket in report["by_experiment"].items():
        bucket["powered"] = bucket["clusters"] >= MIN_EXPERIMENT_CLUSTERS
        bucket['assignment_provenance'] = provenance[label]
    return {"schema": 1, "start": start, "end": end, "mature_before": mature_end,
            "live": report["by_experiment"], "counterfactual": shadows,
            "by_lane": report["by_lane"], "by_mode": report["by_mode"],
            "min_clusters_per_arm": MIN_EXPERIMENT_CLUSTERS,
            "uncertainty": "Wilson 95% intervals on epoch/mode clusters, repeated views pooled",
            "promotion_rule": "Affirmative controlled ranker assignment with nonzero probability for both arms; "
                              "100 mature clusters per arm; disjoint acceptance intervals; "
                              "no worse sustained-listening or skip intervals",
            "counterfactual_outcomes": None}


def summarize(inputs, start, end, outcome_window_h=24, min_impressions=100):
    """Attribute outcomes before calculating descriptive rates and clustered intervals.

    Ownership was loaded across all relevant impressions, including those outside
    this report. Generated and unknown-provenance plays remain separate from
    organic preference evidence. This function uses no connection or repository.
    """
    impressions, attribution = inputs
    states = {r["id"]: {"played": False, "organic": False, "generated": False,
                         "unknown_provenance": False, "skip": False,
                         "completion": False, "open": False, "start": False,
                         "save": False, "promotion": False, "conversion": False,
                         "play_depth": None} for r in impressions}
    candidates = {r["id"]: json.loads(r["provenance"]).get("candidate", {}) for r in impressions}

    for play in attribution.plays:
        state = states.get(attribution.nearest(play["identity_key"], play["played_at"]))
        if state is None:
            continue
        state["played"] = True
        if play["is_generated"] == 1:
            state["generated"] = True
        elif play["is_generated"] == 0:
            state["organic"] = True
        else:
            state["unknown_provenance"] = True

    for event in attribution.responses:
        owner = attribution.nearest(event["identity_key"], event["at"])
        state = states.get(owner)
        if state is None:
            continue
        state[event["kind"]] = True
        if (candidates[owner].get("ownership") == "cold"
                and (event["kind"] == "promotion"
                     or event["kind"] == "save" and not event["is_generated"])):
            state["conversion"] = True

    # Resolve video-only events from the identity saved when the recommendation was offered.
    # Discovery GC and later catalog edits must not erase or reassign historical responses.
    # Older impressions have no snapshot, so retain their catalog lookup as a fallback.
    for event in attribution.exits:
        key = attribution.exit_key(event['video_id'], event['at'])
        if key is None:
            continue
        state = states.get(attribution.nearest(key, event["at"]))
        if state is None:
            continue
        outcome = classify_exit(event["position"], event["duration"])
        if outcome in ("skip", "completion"):
            state[outcome] = True
        if event["duration"] and event["duration"] > 0 and event["position"] is not None:
            depth = max(0.0, min(1.0, event["position"] / event["duration"]))
            state["play_depth"] = max(state["play_depth"] or 0, depth)

    n = len(impressions)
    counts = {name: sum(1 for state in states.values() if state[name])
              for name in ("played", "organic", "generated", "unknown_provenance", "skip", "completion",
                           "open", "start", "save", "promotion", "conversion")}

    def breakdown(label_for):
        groups = {}
        for imp in impressions:
            label = label_for(imp) or "unknown"
            bucket = groups.setdefault(label, {"impressions": 0, **{name: 0 for name in counts}})
            bucket.setdefault("_rows", []).append(imp)
            bucket["impressions"] += 1
            for outcome in counts:
                bucket[outcome] += int(states[imp["id"]][outcome])
        for bucket in groups.values():
            rows = bucket.pop("_rows")
            metadata = [candidates[r["id"]] for r in rows]
            depths = [states[r["id"]]["play_depth"] for r in rows
                      if states[r["id"]]["play_depth"] is not None]
            ages = [max(0, r["served_at"] - candidates[r["id"]]["last_played"]) / 86400
                    for r in rows if candidates[r["id"]].get("last_played") is not None]
            cold = sum(c.get("ownership") == "cold" for c in metadata)
            unique = len({r["identity_key"] for r in rows})
            bucket["acceptance_rate"] = bucket["played"] / bucket["impressions"]
            bucket["completion_rate"] = bucket["completion"] / bucket["impressions"]
            bucket["skip_rate"] = bucket["skip"] / bucket["impressions"]
            bucket["cold_impressions"] = cold
            bucket["discovery_conversion_rate"] = bucket["conversion"] / cold if cold else None
            bucket["unique_items"] = unique
            bucket["repeat_rate"] = (len(rows) - unique) / len(rows)
            bucket["unique_artists"] = len({c["artist"] for c in metadata if c.get("artist")})
            bucket["unique_genres"] = len({c["genre"] for c in metadata if c.get("genre")})
            bucket["mean_play_depth"] = statistics.mean(depths) if depths else None
            bucket["play_depth_samples"] = len(depths)
            bucket["median_days_since_play"] = statistics.median(ages) if ages else None
            bucket["staleness_samples"] = len(ages)
            bucket["never_played"] = sum(c.get("last_played") is None and "last_played" in c for c in metadata)
            bucket["powered"] = bucket["impressions"] >= int(min_impressions)
            bucket["verdict"] = None
            clusters = {}
            opportunities = set()
            for row in rows:
                experiment = json.loads(row["provenance"]).get("experiment") or {}
                if not experiment:
                    continue
                opportunities.add(experiment["comparison_id"])
                cluster = clusters.setdefault(experiment["cluster"],
                    {"played": False, "completion": False, "skip": False})
                for metric in cluster:
                    cluster[metric] |= states[row["id"]][metric]
            bucket["opportunities"] = len(opportunities)
            bucket["clusters"] = len(clusters)
            bucket["cluster_rates"] = {}
            bucket["cluster_ci95"] = {}
            for metric in ("played", "completion", "skip"):
                hits = sum(cluster[metric] for cluster in clusters.values())
                bucket["cluster_rates"][metric] = hits / len(clusters) if clusters else None
                bucket["cluster_ci95"][metric] = _wilson(hits, len(clusters))
        return groups

    unique_keys = {r["identity_key"] for r in impressions}
    repeats = n - len(unique_keys)
    powered = n >= int(min_impressions)
    depths = [s["play_depth"] for s in states.values() if s["play_depth"] is not None]
    staleness = [max(0, r["served_at"] - candidates[r["id"]]["last_played"]) / 86400
                 for r in impressions if candidates[r["id"]].get("last_played") is not None]
    cold = sum(c.get("ownership") == "cold" for c in candidates.values())
    return {
        "start": start, "end": end, "outcome_window_h": outcome_window_h,
        "impressions": n, **counts,
        "acceptance_rate": counts["played"] / n if n else None,
        "organic_rate": counts["organic"] / n if n else None,
        "skip_rate": counts["skip"] / n if n else None,
        "completion_rate": counts["completion"] / n if n else None,
        # Acceptance is the north star because a live play is the broadest reliable response
        # sensor today. Completion and skip are guardrails; organic_rate is evidence suitable
        # for taste learning, while generated response is product efficacy only.
        "north_star": "rendered_to_played",
        "guardrails": ["skip_rate", "completion_rate", "repeat_rate", "unique_artists", "unique_genres"],
        "open_rate": counts["open"] / n if n else None,
        "save_rate": counts["save"] / n if n else None,
        "cold_impressions": cold,
        "discovery_conversion_rate": counts["conversion"] / cold if cold else None,
        "mean_play_depth": statistics.mean(depths) if depths else None,
        "play_depth_samples": len(depths),
        "unique_artists": len({c["artist"] for c in candidates.values() if c.get("artist")}),
        "unique_genres": len({c["genre"] for c in candidates.values() if c.get("genre")}),
        "median_days_since_play": statistics.median(staleness) if staleness else None,
        "staleness_samples": len(staleness),
        "never_played": sum(c.get("last_played") is None and "last_played" in c for c in candidates.values()),
        "unique_items": len(unique_keys), "repeat_rate": repeats / n if n else None,
        "by_surface": breakdown(lambda r: r["surface"]),
        "by_lane": breakdown(lambda r: r["lane"]),
        "by_mode": breakdown(lambda r: str(r["mode_id"]) if r["mode_id"] is not None else "unknown"),
        "by_ranker": breakdown(lambda r: json.loads(r["provenance"]).get("ranker")),
        "by_experiment": breakdown(lambda r:
            ((json.loads(r["provenance"]).get("experiment") or {}).get("cohort", "unknown") + ":" +
             json.loads(r["provenance"]).get("ranker", "unknown"))),
        "by_ownership": breakdown(lambda r: candidates[r["id"]].get("ownership")),
        "by_metadata": {axis: breakdown(lambda r, axis=axis:
            ("present" if candidates[r["id"]]["coverage"].get(axis) else "missing")
            if "coverage" in candidates[r["id"]] else "unknown") for axis in ("genre", "year", "audio")},
        "min_impressions": int(min_impressions), "powered": powered,
        # A volume threshold can make a descriptive rate stable; it cannot manufacture a
        # counterfactual. #110's controlled comparison may add a verdict later.
        "verdict": None,
    }
def _wilson(hits, n):
    if not n:
        return None
    z = 1.96
    p = hits / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, center - radius), min(1, center + radius)]

