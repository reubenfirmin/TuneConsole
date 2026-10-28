"""Foreground action lookup and reusable, bounded experiment policy snapshots."""
import json

import pytest

from yt_playlist.rec import measurement, scorecard


@pytest.mark.parametrize("membership", ["library", "discovery", "cached", "unknown"])
def test_action_resolution_preserves_membership_without_loading_pool(store, monkeypatch, membership):
    if membership == "library":
        store.tracks.upsert_track("target", "Target", "Artist", None, 100)
    elif membership == "discovery":
        store.discovery.upsert_discovered_track(
            "target|artist", "target", "Target", "Artist", "", None, None, None, "source", 1)
    elif membership == "cached":
        store.tracks.observe_track("target", "Target", "Artist", now=1)
    for i in range(20):
        store.discovery.upsert_discovered_track(
            f"other {i}|artist", f"other-{i}", f"Other {i}", "Artist", "", None, None, None, "source", 1)

    def unexpected(*args, **kwargs):
        pytest.fail("action lookup must not materialize discovery metadata")

    monkeypatch.setattr(store.discovery, "get_discovered_tracks", unexpected)
    monkeypatch.setattr(store.discovery, "_dtrack_row", unexpected)
    if membership == "library":
        monkeypatch.setattr(store.discovery, "identity_keys_for_video", unexpected)
    for _ in range(2):
        measurement.record_action(store, "start", 100, video_id="target", event_id="action", generated=True)
    rows = store.conn.execute("SELECT identity_key,is_generated FROM rec_response_events").fetchall()
    assert [tuple(row) for row in rows] == ([('target|artist', 1)] if membership in {"library", "discovery"} else [])


def _offer(store, opportunity, now=100, share=.5):
    measurement.record_recommendations(store, "home", [{"key": "song|artist"}], now,
        request_id=opportunity, ranker="ppr", experiment={
            "name": "comparison", "cohort": "stable", "share": share,
            "comparison_id": opportunity, "cluster": opportunity + ":mode"})


def test_discovery_video_lookup_uses_membership_primary_key(store):
    store.discovery.upsert_discovered_track(
        "target|artist", "target", "Target", "Artist", "", None, None, None, "source", 1)
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        assert store.discovery.identity_keys_for_video("target") == ["target|artist"]
    finally:
        store.conn.set_trace_callback(None)
    query = next(sql for sql in statements if "SELECT DISTINCT r.identity_key" in sql)
    plan = [row[3] for row in store.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert not any("SCAN d" in row or "SCAN e" in row for row in plan), plan
    assert any("SEARCH d USING INTEGER PRIMARY KEY" in row for row in plan), plan


def test_experiment_policy_reuses_definition_and_retains_opportunity_linkage(store):
    for i in range(30):
        _offer(store, f"opportunity-{i}")
    assert store.conn.execute("SELECT COUNT(*) FROM rec_serving_policies").fetchone()[0] == 1
    rows = store.conn.execute("SELECT id FROM rec_served_impressions ORDER BY id").fetchall()
    assert len(rows) == 30
    for i, row in enumerate(rows):
        trace = scorecard.served_recommendation(store, row[0])
        assert trace["configuration"]["experiment"] == {"name": "comparison", "cohort": "stable", "share": .5}
        assert trace["provenance"]["experiment"]["comparison_id"] == f"opportunity-{i}"
        assert trace["provenance"]["experiment"]["cluster"] == f"opportunity-{i}:mode"
    _offer(store, "changed-definition", share=.25)
    assert store.conn.execute("SELECT COUNT(*) FROM rec_serving_policies").fetchone()[0] == 2


def test_policy_captures_aggregate_evidence_without_lifetime_playlist_handles(store, monkeypatch):
    store.modes.replace_modes([{"mode_id": 7, "label": "Rock", "families": [],
        "centroid": [1., 0.], "size": 1, "rep_keys": []}], [], 1)
    for i in range(100):
        store.modes.log_pick(i + 1, 7, i + 1)
    store.modes.log_impressions(1, [("wheelhouse", 7)], 1)

    def unexpected(*args, **kwargs):
        pytest.fail("snapshot must aggregate picks in SQL")

    monkeypatch.setattr(type(store.modes), "pick_rows", unexpected)
    version, config = measurement.serving_policy(store, 100)
    assert config["modes"]["picks"] == {7: 100}
    for i in range(100, 200):
        store.modes.log_pick(i + 1, 7, i + 1)
    changed, updated = measurement.serving_policy(store, 100)
    assert changed != version
    assert updated["modes"]["picks"] == {7: 200}
    assert len(json.dumps(updated)) == len(json.dumps(config))
    assert store.modes.pick_counts(50, before=100) == {7: 50}


def test_reused_old_policy_remains_traceable_until_last_reference_expires(store):
    _offer(store, "old", now=1)
    _offer(store, "recent", now=100)
    store.rec_ledger._prune_impression_batch(50)
    store.rec_ledger._prune_policy_batch(50)
    impression = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()[0]
    assert scorecard.served_recommendation(store, impression)["configuration"]["experiment"]["cohort"] == "stable"
    store.rec_ledger._prune_impression_batch(101)
    store.rec_ledger._prune_policy_batch(101)
    assert store.conn.execute("SELECT COUNT(*) FROM rec_serving_policies").fetchone()[0] == 0
