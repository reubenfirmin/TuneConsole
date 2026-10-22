"""#113 rendered recommendation opportunities and causal-ish outcome attribution."""

import pytest

from yt_playlist.web.routes.home import _record_served_cards
from yt_playlist.rec import measurement


def _shown(store, request, at, key="song|artist", rank=1, surface="home"):
    store.rec_surface.record_served_impressions(request, surface, [{
        "lane": "wheelhouse", "identity_key": key, "rank": rank,
        "model_version": "model-a", "provenance": {"ranker": "cosine"},
    }], at)


def _seed_track(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("vid", "song", "artist", None, None)
    return iid


def test_record_is_idempotent_and_prunes(store):
    _shown(store, "old", 10)
    _shown(store, "old", 10)  # HTMX retry of the same request/rank
    _shown(store, "new", 100)
    store.rec_surface.record_served_impressions("noop", "home", [], 101, prune_before=50)
    rows = store.conn.execute("SELECT request_id FROM rec_served_impressions ORDER BY served_at").fetchall()
    assert [r["request_id"] for r in rows] == ["new"]


def test_outcome_goes_to_nearest_preceding_impression(store):
    iid = _seed_track(store)
    _shown(store, "first", 100)
    _shown(store, "second", 150)
    store.history.record_play_event(iid, "song|artist", "vid", 160)
    store.player_events.record_player_event(iid, "track_exit", "vid", 190, 200, None, None, 170)

    score = store.rec_surface.recommendation_scorecard(90, 200, min_impressions=1)
    assert score["impressions"] == 2
    assert score["played"] == score["organic"] == 1
    assert score["completion"] == 1
    assert score["powered"] is True


@pytest.mark.parametrize("position,outcome", [(20, "skip"), (190, "completion")])
def test_report_boundaries_do_not_change_play_or_exit_ownership(store, position, outcome):
    iid = _seed_track(store)
    _shown(store, "first", 100)
    _shown(store, "second", 150, surface="explore")
    store.history.record_play_event(iid, "song|artist", "vid", 160)
    store.player_events.record_player_event(iid, "track_exit", "vid", position, 200, None, None, 170)

    first = store.rec_surface.recommendation_scorecard(90, 150)
    second = store.rec_surface.recommendation_scorecard(150, 200)
    combined = store.rec_surface.recommendation_scorecard(90, 200)
    assert first["impressions"] == second["impressions"] == 1
    for metric in ("played", "organic", outcome):
        assert first[metric] == 0
        assert second[metric] == combined[metric] == 1
    assert combined["by_surface"]["home"]["played"] == 0
    assert combined["by_surface"]["explore"]["played"] == 1


def test_equal_timestamp_outcome_belongs_to_last_served_impression(store):
    iid = _seed_track(store)
    _shown(store, "first", 100)
    _shown(store, "second", 100, surface="explore")
    store.history.record_play_event(iid, "song|artist", "vid", 120)
    score = store.rec_surface.recommendation_scorecard(90, 110)
    assert score["played"] == 1
    assert score["by_surface"]["home"]["played"] == 0
    assert score["by_surface"]["explore"]["played"] == 1


def test_generated_response_is_not_organic_and_underpowered_has_no_verdict(store):
    iid = _seed_track(store)
    store.playlists.set_playlist_group('PLGEN','Generated')
    store.playlists.mark_generated_playlist('PLGEN')
    store.conn.commit()
    _shown(store, "one", 100)
    store.history.record_play_event(iid, "song|artist", "vid", 120, playlist_ytm_id="RDAMPLPLGEN")

    score = store.rec_surface.recommendation_scorecard(90, 200)
    assert score["played"] == score["generated"] == 1
    assert score["organic"] == 0
    assert score["powered"] is False
    assert score["verdict"] is None


@pytest.mark.parametrize("playlist", ["PLGEN", "RDAMPLPLGEN"])
@pytest.mark.parametrize("new_group", ["", "Favorites"])
def test_play_time_provenance_survives_promotion_and_sensor_rereports(store, playlist, new_group):
    iid = _seed_track(store)
    store.playlists.set_playlist_group("PLGEN", "Generated")
    store.playlists.mark_generated_playlist('PLGEN')
    _shown(store, "one", 100)
    store.history.record_play_event(iid, "song|artist", "vid", 120, playlist_ytm_id=playlist)
    before = store.rec_surface.recommendation_scorecard(90, 200)

    store.playlists.set_playlist_group("PLGEN", new_group)
    assert store.history.record_play_event(iid, "song|artist", "vid", 125,
                                   playlist_ytm_id=playlist, like_status="LIKE") is False
    assert store.rec_surface.recommendation_scorecard(90, 200) == before
    assert before["generated"] == 1 and before["organic"] == 0

    # A later play after a group change is still quarantined until explicit adoption.
    assert store.history.record_play_event(iid, "song|artist", "vid", 5000, playlist_ytm_id=playlist) is True
    assert store.rec_surface.recommendation_scorecard(90, 200) == before

    # Explicit adoption changes future plays, while preserving the original event's policy.
    store.playlists.adopt_generated_playlist('PLGEN')
    assert store.history.record_play_event(iid, "song|artist", "vid", 10000, playlist_ytm_id=playlist) is True
    score = store.rec_surface.recommendation_scorecard(90, 200)
    assert score["generated"] == score["organic"] == 1
    assert [r[0] for r in store.conn.execute('SELECT is_generated FROM play_events ORDER BY id')]==[1,1,0]


def test_imported_historical_play_is_response_with_unknown_provenance(store):
    iid = _seed_track(store)
    _shown(store, "one", 100)
    store.history.import_play_events(iid, [("song|artist", "vid", 120)])
    score = store.rec_surface.recommendation_scorecard(90, 200)
    assert score["played"] == score["unknown_provenance"] == 1
    assert score["generated"] == score["organic"] == 0


def test_outcome_window_and_impression_date_range_are_bounded(store):
    iid = _seed_track(store)
    _shown(store, "outside", 50)
    _shown(store, "inside", 100)
    store.history.record_play_event(iid, "song|artist", "vid", 100 + 25 * 3600)

    score = store.rec_surface.recommendation_scorecard(90, 110, outcome_window_h=24)
    assert score["impressions"] == 1
    assert score["played"] == 0


@pytest.mark.parametrize("offset,played", [(-1, 0), (0, 1), (3600, 1), (3601, 0)])
def test_outcome_window_includes_its_exact_boundaries(store, offset, played):
    iid = _seed_track(store)
    _shown(store, "one", 100)
    store.history.record_play_event(iid, "song|artist", "vid", 100 + offset)
    score = store.rec_surface.recommendation_scorecard(90, 110, outcome_window_h=1)
    assert score["played"] == played


def test_home_records_final_card_order_and_trace(store):
    protos = [{"lane": "explore", "mode_id": 7, "ranker": "ppr",
               "recipe": {"model": "mode"},
               "tracks": [{"key": "a|x"}, {"key": "b|y"}]}]
    _record_served_cards(store, protos, 100)
    rows = store.conn.execute(
        "SELECT lane,mode_id,identity_key,rank,model_version,provenance "
        "FROM rec_served_impressions ORDER BY rank").fetchall()
    assert [(r["identity_key"], r["rank"]) for r in rows] == [("a|x", 1), ("b|y", 2)]
    assert rows[0]["lane"] == "explore" and rows[0]["mode_id"] == 7
    assert len(rows[0]["model_version"]) == 16
    assert '"ranker": "ppr"' in rows[0]["provenance"]


def test_coverage_conversion_and_variety_use_immutable_serving_metadata(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tid = store.tracks.upsert_track("owned", "Owned", "Known artist", None, 200)
    store.tracks.set_track_genre(tid, "Techno")
    store.tracks.set_track_year(tid, "1999")
    pid = store.playlists.upsert_playlist(iid, "PL", "Library", 1, "h", 0)
    store.playlists.set_playlist_tracks(pid, [tid])
    store.discovery.upsert_discovered_track("cold|new artist", "cold", "Cold", "New artist", "", None,
                                  None, "2000", "source", 0)
    measurement.record_recommendations(store, "home", [
        {"key": "owned|known artist", "score": .9}, {"key": "cold|new artist", "score": .8}], 100)
    store.rec_surface.record_recommendation_outcomes("save-generated", "save", ["cold|new artist"], 110, generated=True)
    before = store.rec_surface.recommendation_scorecard(90, 105)
    assert before["save"] == 1 and before["conversion"] == 0
    store.rec_surface.record_recommendation_outcomes("promote", "promotion", ["cold|new artist"], 120)
    store.rec_surface.record_recommendation_outcomes("promote", "promotion", ["cold|new artist"], 120)
    score = store.rec_surface.recommendation_scorecard(90, 105)
    assert score["impressions"] == 2 and score["cold_impressions"] == 1
    assert score["conversion"] == score["promotion"] == 1
    assert score["discovery_conversion_rate"] == 1
    assert score["unique_artists"] == 2 and score["unique_genres"] == 1
    assert score["by_metadata"]["genre"]["missing"]["impressions"] == 1
    assert score["by_metadata"]["year"]["present"]["impressions"] == 2
    assert score["by_ownership"]["cold"]["conversion"] == 1
    assert score["by_surface"]["home"]["unique_artists"] == 2
    # Enrichment and ownership change later, but not the historical cohort/slices.
    store.discovery.set_discovered_enrichment("cold|new artist", "Folk", "2000")
    adopted = store.tracks.upsert_track("cold", "Cold", "New artist", None, 200)
    store.playlists.set_playlist_tracks(pid, [tid, adopted])
    assert store.rec_surface.recommendation_scorecard(90, 105) == score
    assert store.conn.execute("SELECT COUNT(*) FROM rec_response_events").fetchone()[0] == 2


@pytest.mark.parametrize("offset,credited", [(-1, 0), (0, 1), (3600, 1), (3601, 0)])
def test_explicit_outcomes_follow_bounded_nearest_ownership(store, offset, credited):
    _shown(store, "first", 50)
    _shown(store, "second", 100, surface="similar")
    store.rec_surface.record_recommendation_outcomes("open", "open", ["song|artist"], 100 + offset)
    store.rec_surface.record_recommendation_outcomes("save", "save", ["song|artist"], 100 + offset)
    score = store.rec_surface.recommendation_scorecard(100, 110, outcome_window_h=1)
    assert score["open"] == score["save"] == credited
    assert store.rec_surface.recommendation_scorecard(40, 100, outcome_window_h=1)["save"] == (offset == -1)


def test_cold_exit_depth_and_staleness_have_samples_per_surface(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.discovery.upsert_discovered_track("cold|artist", "cold", "Cold", "Artist", "", None,
                                  "Techno", None, "source", 0)
    measurement.record_recommendations(store, "similar", [{"key": "cold|artist"}], 100)
    store.player_events.record_player_event(iid, "track_exit", "cold", 180, 200, None, None, 120)
    score = store.rec_surface.recommendation_scorecard(90, 110)
    assert score["completion"] == 1
    assert score["mean_play_depth"] == pytest.approx(.9)
    assert score["by_surface"]["similar"]["play_depth_samples"] == 1
    assert score["by_surface"]["similar"]["never_played"] == 1
    assert score["by_surface"]["similar"]["powered"] is False
    assert score["by_surface"]["similar"]["verdict"] is None


@pytest.mark.parametrize("position,outcome", [(20, "skip"), (190, "completion")])
def test_cold_exit_attribution_survives_discovery_removal_and_report_boundaries(store, position, outcome):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    key = "cold|artist"
    store.discovery.upsert_discovered_track(key, "cold", "Cold", "Artist", "", None,
                                  "Techno", None, "source", 0)
    measurement.record_recommendations(store, "home", [{"key": key}], 100)
    measurement.record_recommendations(store, "similar", [{"key": key}], 150)
    store.player_events.record_player_event(iid, "track_exit", "cold", position, 200, None, None, 170)
    before = store.rec_surface.recommendation_scorecard(90, 160)
    assert before[outcome] == 1 and before["by_surface"]["similar"][outcome] == 1

    with store.conn:
        store.conn.execute("DELETE FROM discovered_tracks WHERE identity_key=?", (key,))
    assert store.discovery.get_discovered_tracks() == []
    assert store.rec_surface.recommendation_scorecard(90, 160) == before
    assert store.rec_surface.recommendation_scorecard(90, 110)[outcome] == 0
    assert store.rec_surface.recommendation_scorecard(150, 160)[outcome] == 1


def test_retention_prunes_responses_and_only_orphaned_old_policies(store):
    measurement.record_recommendations(store, "similar", [{"key": "a|artist"}], 10)
    store.rec_surface.record_recommendation_outcomes("old", "open", ["a|artist"], 20)
    store.rec_surface.record_serving_policy("orphan", {"old": True}, 1)
    measurement.record_recommendations(store, "similar", [{"key": "b|artist"}], 100)
    store.rec_surface.record_served_impressions("prune", "home", [], 101, prune_before=50)
    assert store.conn.execute("SELECT COUNT(*) FROM rec_response_events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM rec_serving_policies WHERE model_version='orphan'").fetchone()[0] == 0
    row = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()
    assert store.rec_surface.served_recommendation(row["id"])["configuration"] is not None
