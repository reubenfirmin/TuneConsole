import json

import pytest

from yt_playlist.rec import experiments, measurement, mode_surfaces, rec_params, scorecard
from yt_playlist.rec.home_cards import _record_served_cards
from tests.test_ppr_assemble import _seed_one_mode
from tests.test_taste_health import _client

_MISSING = object()


def _record(store, request, kind="live", ranker="cosine", at=100, cluster="1:1", key="song|band",
            controlled=_MISSING):
    experiment = {"comparison_id": request.split(":")[0], "cohort": "c1", "cluster": cluster}
    if controlled is not _MISSING:
        experiment['ranker_controlled'] = controlled
    store.rec_ledger.record_served_impressions(request, "home", [{
        "key": key, "rank": 1, "lane": "wheelhouse", "model_version": "m1",
        "provenance": {"ranker": ranker, "policy_kind": kind, "propensity": .5 if kind == "live" else 0,
                       "candidate": {"video_id": "v1"},
                       "experiment": experiment},
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
    assert scorecard.recommendation_scorecard(store, 0, 20)["impressions"] == 6
    assert store.rec_ledger.lane_impression_counts() == {"wheelhouse": 6}
    assert scorecard.served_recommendation(store, rows[-1]["id"])["outcomes"] is None


def test_shadow_cannot_steal_actions_or_listening(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    _record(store, "pair:live")
    _record(store, "pair:shadow", "counterfactual", "ppr", at=110)
    store.history.import_play_events(iid, [("song|band", "v1", 120)])
    store.rec_ledger.record_recommendation_outcomes("save", "save", ["song|band"], 121)
    score = scorecard.recommendation_scorecard(store, 90, 130)
    assert score["impressions"] == score["played"] == score["save"] == 1
    assert set(score["by_ranker"]) == {"cosine"}
    report = scorecard.experiment_report(store, 90, 130, 100000)
    assert report["live"]["c1:cosine"]["played"] == 1
    assert report["counterfactual"] == [{"ranker": "ppr", "rows": 1, "opportunities": 1}]
    assert report["counterfactual_outcomes"] is None


def test_repeat_views_do_not_manufacture_independent_samples(store):
    for i in range(101):
        _record(store, str(i), at=100 + i)
    bucket = scorecard.experiment_report(store, 90, 300, 100000)["live"]["c1:cosine"]
    assert bucket["opportunities"] == 101
    assert bucket["clusters"] == 1
    assert not bucket["powered"]
    assert bucket["cluster_ci95"]["played"][1] > .7
    with pytest.raises(ValueError, match="100 mature"):
        experiments.decide(store, "promote", "c1", "looks promising", 100000)


def test_pending_outcomes_are_not_promotion_evidence(store):
    _record(store, "new", at=1000)
    assert scorecard.experiment_report(store, 900, 1100, 1200)["live"] == {}


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
    assert set(scorecard.recommendation_scorecard(store, 0, 20)["by_ranker"]) == {"cosine"}


def test_cached_solver_provenance_survives_tuning_and_identical_rebuild(store):
    _seed_one_mode(store)
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    policy = {"algorithm": "basket-ppr-v1", "built_at": 1,
              "params": {"ppr_alpha": .75, "ppr_iters": 30}, "ranking_fingerprint": "fixed"}
    bundles["_meta"]["ppr_policy"] = policy
    store.rec_proposals.put_proposals("mode_bundles", bundles, 1)
    rec_params.set_param(store, "ppr_alpha", .9)
    first = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert first["ppr_policy"]["params"]["ppr_alpha"] == .75
    policy["built_at"] = 2
    store.rec_proposals.put_proposals("mode_bundles", bundles, 2)
    second = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert second["cohort"] == first["cohort"]


def test_retention_prunes_both_policies_and_versions(store):
    _record(store, "old:live")
    _record(store, "old:shadow", "counterfactual", "ppr")
    store.rec_ledger.record_serving_policy("m1", {"schema": 3}, 100)
    store.rec_ledger.record_served_impressions("prune", "home", [], 1000, prune_before=500)
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


@pytest.fixture
def mature_live_arms(store):
    base = 100 * 86400
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.set_playlist_group("PLGEN", "Generated")
    store.playlists.mark_generated_playlist('PLGEN')
    for i in range(100):
        for arm in ("ppr", "cosine"):
            key, at = f"{arm}{i}|band", base + 100 + i * 10
            _record(store, f"{arm}{i}", ranker=arm, at=at, cluster=f"{i}:1", key=key, controlled=True)
            store.tracks.upsert_track(key, f"{arm}{i}", "band", None, 200)
            # Distinct video snapshots keep exit attribution independent of today's catalog.
            store.conn.execute("UPDATE rec_served_impressions SET provenance=json_set(provenance,"
                               "'$.candidate.video_id',?) WHERE request_id=?", (key, f"{arm}{i}"))
            store.conn.commit()
            if arm == "ppr":
                store.history.record_play_event(iid, key, key, at + 1, playlist_ytm_id="PLGEN")
            store.player_events.record_player_event(iid, "track_exit", key, 190 if arm == "ppr" else 10,
                                      200, None, None, at + 2)
    return base


def test_generated_responses_stay_quarantined_and_support_explicit_live_decision(store, mature_live_arms):
    base = mature_live_arms
    score = scorecard.recommendation_scorecard(store, base + 90, base + 1200)
    assert score["generated"] == 100 and score["organic"] == 0
    result = experiments.decide(store, "promote", "c1", "Mature live evidence meets guardrails", base + 100000)
    assert result["state"] == "retained" and result["decision"]["powered"]
    assert result["decision"]["live_evidence"]["ppr"]["completion"] == 100


@pytest.mark.parametrize('controlled', [_MISSING, None, False, 1, 'true'],
                         ids=['missing', 'null', 'false', 'integer', 'string'])
def test_powered_outcomes_cannot_promote_with_unknown_or_observational_assignment(
        store, mature_live_arms, controlled):
    row = store.conn.execute('SELECT id,provenance FROM rec_served_impressions ORDER BY id LIMIT 1').fetchone()
    trace = json.loads(row['provenance'])
    if controlled is _MISSING:
        trace['experiment'].pop('ranker_controlled')
    else:
        trace['experiment']['ranker_controlled'] = controlled
    store.conn.execute('UPDATE rec_served_impressions SET provenance=? WHERE id=?',
                       (json.dumps(trace), row['id']))
    store.conn.commit()
    now = mature_live_arms + 100000
    report = scorecard.experiment_report(store, now - 90 * 86400, now, now)
    assert all(report['live']['c1:' + arm]['powered'] for arm in ('ppr', 'cosine'))
    assert report['live']['c1:ppr']['assignment_provenance'] == {
        'controlled': 99, 'uncontrolled': 1, 'legacy_cache': 0}
    with pytest.raises(ValueError, match='Affirmative controlled ranker'):
        experiments.decide(store, 'promote', 'c1', 'Favorable outcomes do not establish assignment', now)
    assert not experiments.decisions(store)
    assert rec_params.get_param(store, 'ppr_ab_share') == .5


@pytest.mark.parametrize('probability', [None, 0, 1, -.1, 1.1, True, '.5'])
def test_control_marker_without_valid_randomized_probability_cannot_promote(
        store, mature_live_arms, probability):
    row = store.conn.execute('SELECT id,provenance FROM rec_served_impressions ORDER BY id LIMIT 1').fetchone()
    trace = json.loads(row['provenance'])
    trace['propensity'] = probability
    store.conn.execute('UPDATE rec_served_impressions SET provenance=? WHERE id=?',
                       (json.dumps(trace), row['id']))
    store.conn.commit()
    with pytest.raises(ValueError, match='valid assignment probabilities'):
        experiments.decide(store, 'promote', 'c1', 'Invalid assignment probability', mature_live_arms + 100000)


def test_decision_provenance_uses_only_actual_mature_report_arms(store, mature_live_arms):
    now = mature_live_arms + 100000
    # Legacy and observational rows outside the window, awaiting maturity, or
    # outside the analyzed live arms must not veto a valid controlled cohort.
    _record(store, 'old-legacy', at=100)
    _record(store, 'pending', at=now - 10, controlled=False)
    _record(store, 'shadow', kind='counterfactual', ranker='ppr', at=now - 90000, controlled=False)
    _record(store, 'other-ranker', ranker='another', at=now - 90000, controlled=False)
    store.conn.execute("UPDATE rec_served_impressions SET provenance=json_set(provenance,"
                       "'$.experiment.ppr_policy.algorithm','unknown-legacy-cache') WHERE request_id='old-legacy'")
    store.conn.commit()
    result = experiments.decide(store, 'promote', 'c1', 'Only analyzed evidence supports the decision', now)
    assert result['state'] == 'retained'
    assert result['decision']['live_evidence']['ppr']['assignment_provenance'] == {
        'controlled': 100, 'uncontrolled': 0, 'legacy_cache': 0}


def test_mature_legacy_cache_in_analyzed_arms_cannot_promote(store, mature_live_arms):
    store.conn.execute("UPDATE rec_served_impressions SET provenance=json_set(provenance,"
                       "'$.experiment.ppr_policy.algorithm','unknown-legacy-cache') WHERE request_id='ppr0'")
    store.conn.commit()
    with pytest.raises(ValueError, match='Rebuild the legacy ranking cache'):
        experiments.decide(store, 'promote', 'c1', 'Unknown ranker policy', mature_live_arms + 100000)
