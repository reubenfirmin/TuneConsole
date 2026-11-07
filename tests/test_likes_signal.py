# tests/test_likes_signal.py
from tests.retained_rating_support import apply_model_ratings, observe_rating
from yt_playlist.rec import rec_params
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


def test_combined_ratings_graduate_sync_likes_at_low_weight(store):
    s = store
    rec_params.set_param(s, "source_w_like_synced", .2)
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    # a sync-discovered like (default provenance) feeds the ledger at the LOW sync-like weight:
    # a single one must not cross theme_threshold (1.2) on its own
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0)
    fam = genre_map.family("Techno")
    assert s.rec_model.recent_liked_keys() == ["song|band"]                 # captured
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == .2
    assert rec_params.get_param(s, "source_w_like_synced") < rec_params.get_param(s, "theme_threshold")
    assert s.rec_model.like_provenance("song|band") == "sync"


def test_combined_ratings_graduate_action_likes_at_normal_weight(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0, provenance="action")
    fam = genre_map.family("Techno")
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.get_param(s, "source_w_like")
    assert s.rec_model.like_provenance("song|band") == "action"


def test_combined_likes_idempotent_on_resync(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0)
    apply_model_ratings(s, {"song|band": "LIKE"}, 1001.0)    # re-sync
    fam = genre_map.family("Techno")
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.get_param(s, "source_w_like_synced")  # fed once


def test_provenance_upgrade_wins_and_does_not_regraduate(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    apply_model_ratings(s, {"song|band": "LIKE"}, 1000.0)                       # sync first
    assert s.rec_model.like_provenance("song|band") == "sync"
    apply_model_ratings(s, {"song|band": "LIKE"}, 1001.0, provenance="action")  # re-observed live
    assert s.rec_model.like_provenance("song|band") == "action"            # upgraded
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.get_param(s, "source_w_like_synced")  # not replayed
    # and never downgraded back to sync by a later library sync
    apply_model_ratings(s, {"song|band": "LIKE"}, 1002.0)
    assert s.rec_model.like_provenance("song|band") == "action"


# --- Likes and the transient model: only action-provenance likes feed it ---
from yt_playlist.rec import transient


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


def test_dislike_still_suppresses_and_leans_negative(store):
    s = store
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    apply_model_ratings(s, {"song|band": "DISLIKE"}, 1000.0)
    fam = genre_map.family("Techno")
    assert "song|band" in s.rec_model.suppressed_keys("for_you", 1000.0)   # actioned suppression unchanged
    assert transient.facet_leans(s, 1000.0).get(f"genre:{fam}", 0.0) < 0.0
