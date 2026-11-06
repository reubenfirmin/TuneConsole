import json
import sqlite3

import pytest

from tests.prepared_fixture_support import prepared_template, roots
from yt_playlist.rec import experiments, measurement, mode_surfaces, rec_params, scorecard
from tests.test_ppr_assemble import _seed_one_mode
from tests.recommendation_support import publish_bundles
from tests.test_taste_health import _client

_MISSING = object()


def _record(store, request, kind="live", ranker="cosine", at=100, cluster="1:1", key="song|band",
            controlled=_MISSING, video_id='v1', propensity=_MISSING, ppr_policy=_MISSING):
    experiment = {"comparison_id": request.split(":")[0], "cohort": "c1", "cluster": cluster}
    if controlled is not _MISSING:
        experiment['ranker_controlled'] = controlled
    if ppr_policy is not _MISSING:
        experiment['ppr_policy'] = ppr_policy
    store.rec_ledger.record_served_impressions(request, "home", [{
        "key": key, "rank": 1, "lane": "wheelhouse", "model_version": "m1",
        "provenance": {"ranker": ranker, "policy_kind": kind,
                       "propensity": (.5 if kind == "live" else 0) if propensity is _MISSING else propensity,
                       "candidate": {"video_id": video_id},
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
    assert scorecard.recommendation_scorecard(store, 0, 20)["impressions"] == 0
    assert store.rec_ledger.lane_impression_counts() == {}
    assert card['experiment']['state'] == 'retained'
    assert card['experiment']['share'] == 1


def test_shadow_cannot_steal_actions_or_listening(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
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
    assert scorecard.recommendation_scorecard(store, 0, 20)["by_ranker"] == {}
    assert card['ranker'] == 'cosine' and card['counterfactual']['ranker'] == 'ppr'


def test_cached_solver_provenance_survives_tuning_and_identical_rebuild(store):
    _seed_one_mode(store)
    bundles = store._fixture_bundle_payload
    policy = {"algorithm": "basket-ppr-v1", "built_at": 1,
              "params": {"ppr_alpha": .75, "ppr_iters": 30}, "ranking_fingerprint": "fixed"}
    bundles["_meta"]["ppr_policy"] = policy
    publish_bundles(store, bundles, 1)
    rec_params.set_param(store, "ppr_alpha", .9)
    first = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert first["ppr_policy"]["params"]["ppr_alpha"] == .75
    policy["built_at"] = 2
    publish_bundles(store, bundles, 2)
    second = mode_surfaces.assemble_cards(store, 10, 0)[0]["experiment"]
    assert second["cohort"] == first["cohort"]


def test_retention_prunes_both_policies_and_versions(store):
    _record(store, "old:live")
    _record(store, "old:shadow", "counterfactual", "ppr")
    store.rec_ledger.record_serving_policy("m1", {"schema": 3}, 100)
    store.rec_ledger.record_served_impressions("prune", "home", [], 1000, prune_before=500)
    assert not store.conn.execute("SELECT 1 FROM rec_served_impressions").fetchone()
    assert store.rec_ledger.policy_configuration('m1') is None
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
def mature_live_arms(store, mature_live_arms_template):
    return _mature_live_arms(store, mature_live_arms_template)


@pytest.fixture(scope='module')
def mature_live_arms_template(_pristine_current_schema):
    def seed(store):
        return _seed_mature_live_arms(store, episodes=range(1, 100))
    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


def _mature_live_arms(store, template, **first_assignment):
    base = template.restore(store)
    assert _seed_mature_live_arms(store, episodes=range(1), **first_assignment) == base
    return base


def _seed_mature_live_arms(store, *, episodes=range(100), **first_assignment):
    base = 100 * 86400
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.playlists.set_playlist_group("PLGEN", "Generated")
    store.playlists.mark_generated_playlist('PLGEN')
    for i in episodes:
        for arm in ("ppr", "cosine"):
            key, at = f"{arm}{i}|band", base + 100 + i * 10
            store.tracks.upsert_track(key, f"{arm}{i}", "band", None, 200)
            # Distinct video snapshots keep exit attribution independent of today's catalog.
            assignment = {'controlled': True}
            if i == 0 and arm == 'ppr':
                assignment.update(first_assignment)
            _record(store, f"{arm}{i}", ranker=arm, at=at, cluster=f"{i}:1", key=key,
                    video_id=key, **assignment)
            if arm == "ppr":
                store.history.record_play_event(iid, key, key, at + 1, playlist_ytm_id="PLGEN")
            store.player_events.record_player_event(iid, "track_exit", key, 190 if arm == "ppr" else 10,
                                      200, None, None, at + 2)
    return base


def test_generated_responses_stay_quarantined_and_support_explicit_live_decision(
        store, mature_live_arms_template, file_backed_store, _pristine_current_schema):
    template = mature_live_arms_template
    base = template.restore(store)
    assert template.restore(file_backed_store) == base
    now = base + 100000
    prefix = scorecard.experiment_report(template.source, now - 90 * 86400, now, now)
    assert all(arm['clusters'] == arm['impressions'] == 99 and not arm['powered']
               for arm in prefix['live'].values())
    for current in (store, file_backed_store):
        assert scorecard.experiment_report(current, now - 90 * 86400, now, now) == prefix
        with pytest.raises(ValueError, match='100 mature'):
            experiments.decide(current, 'promote', 'c1', 'The last episode is still missing', now)
        assert not experiments.decisions(current)
    assert len({template.source._evidence_key, store._evidence_key, file_backed_store._evidence_key}) == 3
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        with template.source.conn:
            template.source.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")
    destinations = [roots(target.conn) for target in (store, file_backed_store)]
    copied = 0
    for name, directory in template.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if source.is_file():
                paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                assert all(path.read_bytes() == source.read_bytes() for path in paths)
                assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                copied += 1
    assert copied
    # Add the varied episode through the real APIs; never edit retained receipts.
    assert _seed_mature_live_arms(store, episodes=range(1)) == base
    assert _seed_mature_live_arms(file_backed_store, episodes=range(1), controlled=False) == base
    file_backed_store.settings.set_setting('private_experiment', 'sibling')
    file_backed_store._fixture_bundle_payload['private'] = ['sibling']
    sibling = scorecard.experiment_report(file_backed_store, now - 90 * 86400, now, now)
    assert sibling['live']['c1:ppr']['assignment_provenance'] == {
        'controlled': 99, 'uncontrolled': 1, 'legacy_cache': 0}
    assert scorecard.experiment_report(template.source, now - 90 * 86400, now, now) == prefix
    assert store.settings.get_setting('private_experiment') is None
    assert template.source.settings.get_setting('private_experiment') is None
    assert store._fixture_bundle_payload == template.bundles == {}
    with prepared_template(_pristine_current_schema, _seed_mature_live_arms) as fresh:
        producer = fresh.source
        assert fresh.result == base
        assert scorecard.recommendation_scorecard(store, base + 90, base + 1200) == (
            scorecard.recommendation_scorecard(producer, base + 90, base + 1200))
        report = scorecard.experiment_report(store, now - 90 * 86400, now, now)
        assert report == scorecard.experiment_report(producer, now - 90 * 86400, now, now)
        assert set(report['live']) == {'c1:ppr', 'c1:cosine'}
        assert all(arm['clusters'] == arm['impressions'] == 100 and arm['powered']
                   for arm in report['live'].values())
        for current in (store, producer):
            assert current.player_events.player_events_since(0) == producer.player_events.player_events_since(0)
            assert list(map(tuple, current.conn.execute(
                'SELECT request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at '
                'FROM rec_served_impressions ORDER BY request_id'))) == list(map(tuple, producer.conn.execute(
                'SELECT request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at '
                'FROM rec_served_impressions ORDER BY request_id')))
            assert current.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    score = scorecard.recommendation_scorecard(store, base + 90, base + 1200)
    assert score["generated"] == 100 and score["organic"] == 0
    result = experiments.decide(store, "promote", "c1", "Mature live evidence meets guardrails", base + 100000)
    assert result["state"] == "retained" and result["decision"]["powered"]
    assert result["decision"]["live_evidence"]["ppr"]["completion"] == 100


@pytest.mark.parametrize('controlled', [_MISSING, None, False, 1, 'true'],
                         ids=['missing', 'null', 'false', 'integer', 'string'])
def test_powered_outcomes_cannot_promote_with_unknown_or_observational_assignment(
        store, controlled, mature_live_arms_template):
    base = _mature_live_arms(store, mature_live_arms_template, controlled=controlled)
    now = base + 100000
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
        store, probability, mature_live_arms_template):
    base = _mature_live_arms(store, mature_live_arms_template, propensity=probability)
    with pytest.raises(ValueError, match='valid assignment probabilities'):
        experiments.decide(store, 'promote', 'c1', 'Invalid assignment probability', base + 100000)


def test_decision_provenance_uses_only_actual_mature_report_arms(store, mature_live_arms):
    now = mature_live_arms + 100000
    # Legacy and observational rows outside the window, awaiting maturity, or
    # outside the analyzed live arms must not veto a valid controlled cohort.
    _record(store, 'old-legacy', at=100, ppr_policy={'algorithm': 'unknown-legacy-cache'})
    _record(store, 'pending', at=now - 10, controlled=False)
    _record(store, 'shadow', kind='counterfactual', ranker='ppr', at=now - 90000, controlled=False)
    _record(store, 'other-ranker', ranker='another', at=now - 90000, controlled=False)
    result = experiments.decide(store, 'promote', 'c1', 'Only analyzed evidence supports the decision', now)
    assert result['state'] == 'retained'
    assert result['decision']['live_evidence']['ppr']['assignment_provenance'] == {
        'controlled': 100, 'uncontrolled': 0, 'legacy_cache': 0}


def test_mature_legacy_cache_in_analyzed_arms_cannot_promote(store, mature_live_arms_template):
    base = _mature_live_arms(store, mature_live_arms_template, ppr_policy={'algorithm': 'unknown-legacy-cache'})
    with pytest.raises(ValueError, match='Rebuild the legacy ranking cache'):
        experiments.decide(store, 'promote', 'c1', 'Unknown ranker policy', base + 100000)
