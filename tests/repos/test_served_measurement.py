"""#113 rendered recommendation opportunities and causal-ish outcome attribution."""

from yt_playlist.web.routes.home import _record_served_cards


def _shown(store, request, at, key="song|artist", rank=1):
    store.record_served_impressions(request, "home", [{
        "lane": "wheelhouse", "identity_key": key, "rank": rank,
        "model_version": "model-a", "provenance": {"ranker": "cosine"},
    }], at)


def _seed_track(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("vid", "song", "artist", None, None)
    return iid


def test_record_is_idempotent_and_prunes(store):
    _shown(store, "old", 10)
    _shown(store, "old", 10)  # HTMX retry of the same request/rank
    _shown(store, "new", 100)
    store.record_served_impressions("noop", "home", [], 101, prune_before=50)
    rows = store.conn.execute("SELECT request_id FROM rec_served_impressions ORDER BY served_at").fetchall()
    assert [r["request_id"] for r in rows] == ["new"]


def test_outcome_goes_to_nearest_preceding_impression(store):
    iid = _seed_track(store)
    _shown(store, "first", 100)
    _shown(store, "second", 150)
    store.record_play_event(iid, "song|artist", "vid", 160)
    store.record_player_event(iid, "track_exit", "vid", 190, 200, None, None, 170)

    score = store.recommendation_scorecard(90, 200, min_impressions=1)
    assert score["impressions"] == 2
    assert score["played"] == score["organic"] == 1
    assert score["completion"] == 1
    assert score["powered"] is True


def test_generated_response_is_not_organic_and_underpowered_has_no_verdict(store):
    iid = _seed_track(store)
    store.conn.execute("INSERT INTO playlist_group(ytm,name) VALUES ('PLGEN','Generated')")
    store.conn.commit()
    _shown(store, "one", 100)
    store.record_play_event(iid, "song|artist", "vid", 120, playlist_ytm_id="RDAMPLPLGEN")

    score = store.recommendation_scorecard(90, 200)
    assert score["played"] == score["generated"] == 1
    assert score["organic"] == 0
    assert score["powered"] is False
    assert score["verdict"] is None


def test_outcome_window_and_impression_date_range_are_bounded(store):
    iid = _seed_track(store)
    _shown(store, "outside", 50)
    _shown(store, "inside", 100)
    store.record_play_event(iid, "song|artist", "vid", 100 + 25 * 3600)

    score = store.recommendation_scorecard(90, 110, outcome_window_h=24)
    assert score["impressions"] == 1
    assert score["played"] == 0


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
