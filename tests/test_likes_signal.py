# tests/test_likes_signal.py
from yt_playlist.core.store import Store
from yt_playlist.rec import recommend, rec_params


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_record_like_idempotent_first_seen():
    s = _store()
    assert s.record_like("song|band", 1000.0) is True      # first time -> new
    assert s.record_like("song|band", 1001.0) is False     # re-sync -> idempotent
    assert s.recent_liked_keys() == ["song|band"]


def test_recent_liked_keys_recency_ordered():
    s = _store()
    s.record_like("a|x", 1000.0)
    s.record_like("b|y", 2000.0)
    assert s.recent_liked_keys() == ["b|y", "a|x"]         # newest first
    assert s.recent_liked_keys(limit=1) == ["b|y"]


def test_clear_like_removes_row():
    s = _store()
    s.record_like("a|x", 1000.0)
    s.clear_like("a|x")
    assert s.recent_liked_keys() == []


def test_apply_dislikes_graduates_likes():
    s = _store()
    tid = s.upsert_track("v1", "song", "band", None, None)
    s.set_track_genre(tid, "Techno")
    # a strong "a lot"-equivalent isn't available for likes; a single like is signed +1 at SOURCE_W_LIKE
    recommend.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0)
    fam = recommend.genre_map.family("Techno")
    assert s.recent_liked_keys() == ["song|band"]                 # captured
    assert abs(s.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE   # graduation ledger fed once


def test_apply_dislikes_likes_idempotent_on_resync():
    s = _store()
    tid = s.upsert_track("v1", "song", "band", None, None)
    s.set_track_genre(tid, "Techno")
    recommend.apply_dislikes(s, {"song|band": "LIKE"}, 1000.0)
    recommend.apply_dislikes(s, {"song|band": "LIKE"}, 1001.0)    # re-sync
    fam = recommend.genre_map.family("Techno")
    assert abs(s.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_LIKE   # fed exactly once


# --- Task 3.1: Recent likes feed facet_leans (token channel) ---
from yt_playlist.rec import transient


def test_recent_like_lifts_facet_lean():
    s = _store()
    tid = s.upsert_track("v1", "song", "band", None, None)
    s.set_track_genre(tid, "Techno")
    s.set_setting("last_sync_at", "1000")
    s.record_like("song|band", 1000.0)
    leans = transient.facet_leans(s, 1000.0)
    fam = recommend.genre_map.family("Techno")
    assert leans.get(f"genre:{fam}", 0.0) > 0.0    # the like pushes this facet positive
