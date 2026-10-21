import json

import pytest

from yt_playlist.rec import experiments, measurement, mode_surfaces, rec_params
from yt_playlist.web.routes.home import _record_served_cards
from tests.test_ppr_assemble import _seed_one_mode
from tests.test_taste_health import _client


def _record(store, request, kind="live", ranker="cosine", at=100, cluster="1:1", key="song|band"):
    store.record_served_impressions(request, "home", [{
        "key": key, "rank": 1, "lane": "wheelhouse", "model_version": "m1",
        "provenance": {"ranker": ranker, "policy_kind": kind, "propensity": .5 if kind == "live" else 0,
                       "candidate": {"video_id": "v1"},
                       "experiment": {"comparison_id": request.split(":")[0], "cohort": "c1",
                                      "cluster": cluster}},
    }], at)


def test_pair_replay_and_actual_assignment_propensity(store):
    _seed_one_mode(store)
    rec_params.set_param(store, "ppr_ab_share", 1)
    cards = mode_surfaces.assemble_cards(store, 10, 0)
    card = next(c for c in cards if c["lane"] == "wheelhouse")
    assert card["counterfactual"]["ranker"] == "cosine"
    assert card["experiment"]["candidate_count"] == 6
    assert card["tracks"][0]["key"] == "w5"
    assert card["counterfactual"]["tracks"][0]["key"] == "w0"
    assert not store.conn.execute("SELECT 1 FROM rec_served_impressions").fetchone()
    _record_served_cards(store, [card], 10)
    rows = [dict(r) for r in store.conn.execute("SELECT * FROM rec_served_impressions ORDER BY id")]
    assert len(rows) == 12
    traces = [json.loads(r["provenance"]) for r in rows]
    assert len({p["experiment"]["comparison_id"] for p in traces}) == 1
    assert len({p["experiment"]["candidate_fingerprint"] for p in traces}) == 1
    assert [p["propensity"] for p in traces] == [1] * 6 + [0] * 6
    assert all(p["experiment"]["state"] == "retained" for p in traces)
    assert store.recommendation_scorecard(0, 20)["impressions"] == 6
    assert store.lane_impression_counts() == {"wheelhouse": 6}
    assert store.served_recommendation(rows[-1]["id"])["outcomes"] is None


def test_shadow_cannot_steal_actions_or_listening(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _record(store, "pair:live")
    _record(store, "pair:shadow", "counterfactual", "ppr", at=110)
    store.import_play_events(iid, [("song|band", "v1", 120)])
    store.record_recommendation_outcomes("save", "save", ["song|band"], 121)
    score = store.recommendation_scorecard(90, 130)
    assert score["impressions"] == score["played"] == score["save"] == 1
    assert set(score["by_ranker"]) == {"cosine"}
    report = store.experiment_report(90, 130, 100000)
    assert report["live"]["c1:cosine"]["played"] == 1
    assert report["counterfactual"] == [{"ranker": "ppr", "rows": 1, "opportunities": 1}]
    assert report["counterfactual_outcomes"] is None


def test_repeat_views_do_not_manufacture_independent_samples(store):
    for i in range(101):
        _record(store, str(i), at=100 + i)
    bucket = store.experiment_report(90, 300, 100000)["live"]["c1:cosine"]
    assert bucket["opportunities"] == 101
    assert bucket["clusters"] == 1
    assert not bucket["powered"]
    assert bucket["cluster_ci95"]["played"][1] > .7
    with pytest.raises(ValueError, match="100 mature"):
        experiments.decide(store, "promote", "c1", "looks promising", 100000)


def test_pending_outcomes_are_not_promotion_evidence(store):
    _record(store, "new", at=1000)
    assert store.experiment_report(900, 1100, 1200)["live"] == {}


def test_retirement_is_explicit_and_disables_replay(store):
    _seed_one_mode(store)
    assert experiments.lifecycle(store)["state"] == "live"
    result = experiments.decide(store, "retire", "c1", "Stop collecting", 100000)
    assert result["state"] == "retired"
    assert not result["decision"]["powered"]
    assert "insufficient" in result["decision"]["basis"]
    assert all("counterfactual" not in c for c in mode_surfaces.assemble_cards(store, 10, 0))
    store.conn.execute("DELETE FROM rec_proposals")
    store.conn.commit()
    assert experiments.lifecycle(store)["state"] == "retired"


def test_shadow_default_has_no_live_ppr_outcomes(store):
    _seed_one_mode(store)
    rec_params.set_param(store, "ppr_ab_share", 0)
    card = mode_surfaces.assemble_cards(store, 10, 0)[0]
    assert card["experiment"]["state"] == "shadow"
    _record_served_cards(store, [card], 10)
    assert set(store.recommendation_scorecard(0, 20)["by_ranker"]) == {"cosine"}


def test_cached_solver_provenance_survives_tuning_and_identical_rebuild(store):
    _seed_one_mode(store)
    bundles = store.get_proposals("mode_bundles")
    policy = {"algorithm": "basket-ppr-v1", "built_at": 1,
              "params": {"ppr_alpha": .75, "ppr_iters": 30}, "ranking_fingerprint": "fixed"}
    bundles["_meta"]["ppr_policy"] = policy
    store.put_proposals("mode_bundles", bundles, 1)
    rec_params.set_param(store, "ppr_alpha", .9)
    first = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert first["ppr_policy"]["params"]["ppr_alpha"] == .75
    policy["built_at"] = 2
    store.put_proposals("mode_bundles", bundles, 2)
    second = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert second["cohort"] == first["cohort"]


def test_retention_prunes_both_policies_and_versions(store):
    _record(store, "old:live")
    _record(store, "old:shadow", "counterfactual", "ppr")
    store.record_serving_policy("m1", {"schema": 3}, 100)
    store.record_served_impressions("prune", "home", [], 1000, prune_before=500)
    assert not store.conn.execute("SELECT 1 FROM rec_served_impressions").fetchone()
    assert not store.conn.execute("SELECT 1 FROM rec_serving_policies").fetchone()
    assert not store.conn.execute("SELECT 1 FROM rec_lane_impressions").fetchone()


def test_experiment_endpoints_validate_decisions(store):
    client = _client(store)
    response = client.get("/taste/experiments")
    assert response.status_code == 200 and response.json()["state"] == "live"
    assert client.post("/taste/experiments/decision", data={
        "decision": "promote", "cohort": "x", "reason": "test"}).status_code == 422
    assert client.post("/taste/experiments/decision", data={
        "decision": "retire", "reason": ""}).status_code == 422
    assert client.post("/taste/experiments/decision", data={
        "decision": "retire", "reason": "Stop"}).json()["state"] == "retired"


def test_generated_responses_stay_quarantined_and_support_explicit_live_decision(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.set_playlist_group("PLGEN", "Generated")
    for i in range(100):
        for arm in ("ppr", "cosine"):
            key, at = f"{arm}{i}|band", 100 + i * 10
            _record(store, f"{arm}{i}", ranker=arm, at=at, cluster=f"{i}:1", key=key)
            store.upsert_track(key, f"{arm}{i}", "band", None, 200)
            # Distinct video snapshots keep exit attribution independent of today's catalog.
            store.conn.execute("UPDATE rec_served_impressions SET provenance=json_set(provenance,"
                               "'$.candidate.video_id',?) WHERE request_id=?", (key, f"{arm}{i}"))
            store.conn.commit()
            if arm == "ppr":
                store.record_play_event(iid, key, key, at + 1, playlist_ytm_id="PLGEN")
            store.record_player_event(iid, "track_exit", key, 190 if arm == "ppr" else 10,
                                      200, None, None, at + 2)
    score = store.recommendation_scorecard(90, 1200)
    assert score["generated"] == 100 and score["organic"] == 0
    result = experiments.decide(store, "promote", "c1", "Mature live evidence meets guardrails", 100000)
    assert result["state"] == "retained" and result["decision"]["powered"]
    assert result["decision"]["live_evidence"]["ppr"]["completion"] == 100
