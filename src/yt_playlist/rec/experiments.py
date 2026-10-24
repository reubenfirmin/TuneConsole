"""Explicit local experiment decisions; counterfactual outcomes cannot authorize promotion."""
import json

from yt_playlist.rec import rec_params, scorecard


def decisions(store):
    return json.loads(store.settings.get_setting("ppr_experiment_decisions") or "[]")


def last_decision(store):
    history = decisions(store)
    return history[-1] if history else {}


def lifecycle(store):
    share = float(rec_params.get_param(store, "ppr_ab_share"))
    decision = last_decision(store)
    state = ("retired" if share == 0 and decision.get("decision") == "retire" else
             "shadow" if share == 0 else "retained" if share == 1 else "live")
    return {"state": state, "share": share, "decision": decision}


def decide(store, decision, cohort, reason, now):
    if decision not in {"promote", "retire"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Choose promote or retire and supply a reason.")
    if len(reason) > 2000 or not isinstance(cohort, str) or len(cohort) > 100:
        raise ValueError("Decision fields are too long.")
    with store.read_snapshot() as snapshot:
        rows, record = _prepare_decision(snapshot.store, decision, cohort, reason, now)
        # Resolve cohorts and encode retained evidence off the serving lock.
        # Publication rejects a concurrent evidence/settings change atomically.
        def publish():
            store.conn.executemany("INSERT INTO settings(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", rows)
        if not snapshot.publish(publish):
            raise ValueError("Listening evidence or experiment settings changed. Review the report and try again.")
    return {"state": "retained" if decision == "promote" else "retired",
            "share": 1.0 if decision == "promote" else 0.0, "decision": record}


def _prepare_decision(store, decision, cohort, reason, now):
    # Guard a concurrent allocation edit as well as the evidence/history reads.
    store.settings.get_setting(rec_params.SETTING_PREFIX + "ppr_ab_share")
    report = scorecard.experiment_report(store, max(0, now - 90 * 86400), now, now)
    arms = report["live"]
    ppr, cosine = arms.get(cohort + ":ppr"), arms.get(cohort + ":cosine")
    powered = all(a and a["clusters"] >= report["min_clusters_per_arm"] for a in (ppr, cosine))
    if decision == "promote":
        if not powered:
            raise ValueError("Promotion requires 100 mature epoch/mode clusters in each live arm.")
        if any(arm['assignment_provenance']['uncontrolled']
               or arm['assignment_provenance']['controlled'] != arm['impressions'] for arm in (ppr, cosine)):
            raise ValueError("Affirmative controlled ranker exposure with valid assignment probabilities is required. "
                             "Unknown, legacy and whole-menu allocation evidence remains diagnostic.")
        p, c = ppr["cluster_ci95"], cosine["cluster_ci95"]
        if not (p["played"][0] > c["played"][1] and
                p["completion"][0] >= c["completion"][1] and p["skip"][1] <= c["skip"][0]):
            raise ValueError("Live uncertainty and listening guardrails do not support promotion.")
        if any(arm['assignment_provenance']['legacy_cache'] for arm in (ppr, cosine)):
            raise ValueError("Rebuild the legacy ranking cache before evaluating promotion.")
    record = {"decision": decision, "cohort": cohort, "reason": reason.strip(), "at": now,
              "powered": bool(powered), "live_evidence": {"ppr": ppr, "cosine": cosine},
              "basis": "live outcomes" if powered else "operator retirement; insufficient live evidence"}
    return [("ppr_experiment_decisions", json.dumps((decisions(store) + [record])[-200:])),
            (rec_params.SETTING_PREFIX + "ppr_ab_share", "1.0" if decision == "promote" else "0.0")], record
