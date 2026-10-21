"""Explicit local experiment decisions; counterfactual outcomes cannot authorize promotion."""
import json

from yt_playlist.rec import rec_params
from yt_playlist.repos.base import synchronized


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


@synchronized
def decide(store, decision, cohort, reason, now):
    if decision not in {"promote", "retire"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Choose promote or retire and supply a reason.")
    if len(reason) > 2000 or not isinstance(cohort, str) or len(cohort) > 100:
        raise ValueError("Decision fields are too long.")
    report = store.rec_surface.experiment_report(max(0, now - 90 * 86400), now, now)
    arms = report["live"]
    ppr, cosine = arms.get(cohort + ":ppr"), arms.get(cohort + ":cosine")
    powered = all(a and a["clusters"] >= report["min_clusters_per_arm"] for a in (ppr, cosine))
    if decision == "promote":
        if not powered:
            raise ValueError("Promotion requires 100 mature epoch/mode clusters in each live arm.")
        p, c = ppr["cluster_ci95"], cosine["cluster_ci95"]
        if not (p["played"][0] > c["played"][1] and
                p["completion"][0] >= c["completion"][1] and p["skip"][1] <= c["skip"][0]):
            raise ValueError("Live uncertainty and listening guardrails do not support promotion.")
        legacy = store.conn.execute(
            "SELECT 1 FROM rec_served_impressions WHERE "
            "json_extract(provenance,'$.experiment.cohort')=? AND "
            "json_extract(provenance,'$.experiment.ppr_policy.algorithm')='unknown-legacy-cache' LIMIT 1",
            (cohort,)).fetchone()
        if legacy:
            raise ValueError("Rebuild the legacy ranking cache before evaluating promotion.")
    record = {"decision": decision, "cohort": cohort, "reason": reason.strip(), "at": now,
              "powered": bool(powered), "live_evidence": {"ppr": ppr, "cosine": cosine},
              "basis": "live outcomes" if powered else "operator retirement; insufficient live evidence"}
    # Decision + effective share publish together, independently of disposable rec_proposals caches.
    with store.conn:
        store.conn.executemany("INSERT INTO settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [
                ("ppr_experiment_decisions", json.dumps((decisions(store) + [record])[-200:])),
                (rec_params.SETTING_PREFIX + "ppr_ab_share", "1.0" if decision == "promote" else "0.0")])
    return lifecycle(store)
