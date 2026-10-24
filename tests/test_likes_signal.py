# tests/test_likes_signal.py
from yt_playlist.core.store import Store
from yt_playlist.rec import graduation, rec_params
from yt_playlist.util import genre_map


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_record_like_idempotent_first_seen():
    s = _store()
    assert s.rec_model.record_like("song|band", 1000.0) is True      # first time -> new
    assert s.rec_model.record_like("song|band", 1001.0) is False     # re-sync -> idempotent
    assert s.rec_model.recent_liked_keys() == ["song|band"]


def test_recent_liked_keys_recency_ordered():
    s = _store()
    s.rec_model.record_like("a|x", 1000.0)
    s.rec_model.record_like("b|y", 2000.0)
    assert s.rec_model.recent_liked_keys() == ["b|y", "a|x"]         # newest first
    assert s.rec_model.recent_liked_keys(limit=1) == ["b|y"]


def test_clear_like_removes_row():
    s = _store()
    s.rec_model.record_like("a|x", 1000.0)
    s.rec_model.clear_like("a|x")
    assert s.rec_model.recent_liked_keys() == []


def test_apply_dislikes_graduates_sync_likes_at_low_weight():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    # a sync-discovered like (default provenance) feeds the ledger at the LOW sync-like weight:
    # a single one must not cross theme_threshold (1.2) on its own
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0)
    fam = genre_map.family("Techno")
    assert s.rec_model.recent_liked_keys() == ["song|band"]                 # captured
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE_SYNCED
    assert rec_params.SOURCE_W_LIKE_SYNCED < rec_params.get_param(s, "theme_threshold")
    assert s.rec_model.like_provenance("song|band") == "sync"


def test_apply_dislikes_graduates_action_likes_at_normal_weight():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0, provenance="action")
    fam = genre_map.family("Techno")
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE
    assert s.rec_model.like_provenance("song|band") == "action"


def test_apply_dislikes_likes_idempotent_on_resync():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0)
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1001.0)    # re-sync
    fam = genre_map.family("Techno")
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE_SYNCED   # fed exactly once


def test_provenance_upgrade_wins_and_does_not_regraduate():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0)                       # sync first
    assert s.rec_model.like_provenance("song|band") == "sync"
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1001.0, provenance="action")  # re-observed live
    assert s.rec_model.like_provenance("song|band") == "action"            # upgraded
    assert abs(s.rec_signals.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE_SYNCED   # NOT re-graduated
    # and never downgraded back to sync by a later library sync
    graduation.apply_dislikes(s, {"song|band": "LIKE"}, 1002.0)
    assert s.rec_model.like_provenance("song|band") == "action"


# --- Likes and the transient model: only action-provenance likes feed it ---
from yt_playlist.rec import transient


def test_action_like_lifts_facet_lean():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    s.rec_model.record_like("song|band", 1000.0, provenance="action")
    leans = transient.facet_leans(s, 1000.0)
    fam = genre_map.family("Techno")
    assert leans.get(f"genre:{fam}", 0.0) > 0.0    # a live-observed like pushes this facet positive


def test_sync_like_produces_no_transient_lean():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    s.rec_model.record_like("song|band", 1000.0)             # default provenance: sync-discovered
    fam = genre_map.family("Techno")
    assert transient.facet_leans(s, 1000.0).get(f"genre:{fam}", 0.0) == 0.0
    assert s.rec_model.recent_liked_with_ts() == []          # the transient reader never sees it
    assert s.rec_model.recent_liked_keys() == ["song|band"]  # but the like itself is on record


def test_dislike_still_suppresses_and_leans_negative():
    s = _store()
    tid = s.tracks.upsert_track("v1", "song", "band", None, None)
    s.tracks.set_track_genre(tid, "Techno")
    s.settings.set_setting("last_sync_at", "1000")
    graduation.apply_dislikes(s, {"song|band": "DISLIKE"}, 1000.0)
    fam = genre_map.family("Techno")
    assert "song|band" in s.rec_model.suppressed_keys("for_you", 1000.0)   # actioned suppression unchanged
    assert transient.facet_leans(s, 1000.0).get(f"genre:{fam}", 0.0) < 0.0
