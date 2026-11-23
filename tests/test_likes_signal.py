# tests/test_likes_signal.py
from tests.retained_rating_support import apply_model_ratings, observe_rating
from yt_playlist.rec import rec_params, transient
from yt_playlist.util import genre_map


def test_retained_likes_preserve_first_seen_order_limit_and_neutral_removal(store):
    s = store
    observe_rating(s, 'song|band', 'LIKE', 1000.0)
    observe_rating(s, 'song|band', 'LIKE', 1001.0)
    assert s.rec_model.recent_liked_keys() == ["song|band"]
    assert s.conn.execute("SELECT created_at FROM rec_feedback WHERE kind='like'").fetchone()[0] == 1000.0
    observe_rating(s, 'b|y', 'LIKE', 2000.0)
    assert s.rec_model.recent_liked_keys() == ["b|y", "song|band"]  # newest first
    assert s.rec_model.recent_liked_keys(limit=1) == ["b|y"]
    observe_rating(s, 'song|band', 'INDIFFERENT', 2001.0)
    assert s.rec_model.recent_liked_keys() == ["b|y"]
    observe_rating(s, 'b|y', 'INDIFFERENT', 2002.0)
    assert s.rec_model.recent_liked_keys() == []


def test_combined_sync_like_is_retained_without_recent_or_permanent_influence(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0)
    axis = f"genre:{genre_map.family('Techno')}"
    assert s.rec_model.recent_liked_keys() == ["song|band"]
    assert s.rec_model.like_provenance("song|band") == "sync"
    assert s.rec_model.recent_liked_with_ts() == []
    assert axis not in transient.facet_leans(s, 1000.0)
    assert axis not in s.rec_model.baseline_weights(now=1000.0)


def test_combined_action_like_contributes_its_configured_recent_weight(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0, provenance="action")
    axis = f"genre:{genre_map.family('Techno')}"
    assert s.rec_model.like_provenance("song|band") == "action"
    assert s.rec_model.recent_liked_with_ts() == [("song|band", 1000.0)]
    assert transient.facet_leans(s, 1000.0)[axis] == rec_params.get_param(s, "like_transient_w")
    assert axis not in s.rec_model.baseline_weights(now=1000.0)


def test_combined_likes_idempotent_on_resync(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0)
    original = s.rec_model.repository.rating_rows(["song|band"])
    assert len(original) == 1 and original[0]['created_at'] == 1000.0
    apply_model_ratings(s, {"song|band": "LIKE"}, 1001.0)
    assert s.rec_model.repository.rating_rows(["song|band"]) == original
    assert s.rec_model.recent_liked_with_ts() == []


def test_provenance_upgrade_adds_one_recent_like_and_survives_resync(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    key = "song|band"
    axis = f"genre:{genre_map.family('Techno')}"
    apply_model_ratings(s, {key: "LIKE"}, 1000.0)
    assert s.rec_model.like_provenance("song|band") == "sync"
    assert s.rec_model.recent_liked_with_ts() == []
    apply_model_ratings(s, {key: "LIKE"}, 1001.0, provenance="action")
    assert s.rec_model.like_provenance(key) == "action"
    assert s.rec_model.recent_liked_with_ts() == [(key, 1000.0)]
    lean = transient.facet_leans(s, 1002.0)[axis]
    assert lean > 0.0
    apply_model_ratings(s, {key: "LIKE"}, 1002.0)
    assert s.rec_model.like_provenance(key) == "action"
    assert s.rec_model.recent_liked_with_ts() == [(key, 1000.0)]
    assert transient.facet_leans(s, 1002.0)[axis] == lean
    assert axis not in s.rec_model.baseline_weights(now=1002.0)


def test_action_like_lifts_facet_lean(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    observe_rating(s, 'song|band', 'LIKE', 1000.0, provenance='action')
    leans = transient.facet_leans(s, 1000.0)
    fam = genre_map.family("Techno")
    assert leans.get(f"genre:{fam}", 0.0) > 0.0    # a live-observed like pushes this facet positive


def test_sync_like_produces_no_transient_lean(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    observe_rating(s, 'song|band', 'LIKE', 1000.0)
    fam = genre_map.family("Techno")
    assert transient.facet_leans(s, 1000.0).get(f"genre:{fam}", 0.0) == 0.0
    assert s.rec_model.recent_liked_with_ts() == []          # the transient reader never sees it
    assert s.rec_model.recent_liked_keys() == ["song|band"]  # but the like itself is on record


def test_combined_dislike_suppresses_the_recording_without_a_genre_penalty(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "DISLIKE"}, 1000.0)
    axis = f"genre:{genre_map.family('Techno')}"
    assert s.rec_model.disliked_with_ts() == [("song|band", 1000.0)]
    assert s.rec_model.suppressed_keys("for_you", 1000.0) == {"song|band"}
    assert axis not in transient.facet_leans(s, 1000.0)
    assert axis not in s.rec_model.baseline_weights(now=1000.0)
