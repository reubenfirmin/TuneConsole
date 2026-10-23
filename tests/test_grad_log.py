"""§1c graduation instrumentation: every threshold-crossing graduation logs a rec_grad_log row
(axis, source, ledger value, resulting nudge), so SOURCE_W_* can be tuned against observed behavior
and "did passive plays quietly rewrite taste?" is answerable."""
from yt_playlist.core.store import Store
from yt_playlist.rec import graduation, rec_params
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def _store_with_genre(vid, title, artist, genre):
    s = Store(":memory:")
    s.init_schema()
    tid = s.tracks.upsert_track(vid, title, artist, None, None)
    s.tracks.set_track_genre(tid, genre)
    return s


def test_graduation_logs_the_genre_axis_on_threshold_crossing():
    s = _store_with_genre("v1", "song", "band", "Techno")
    # A strong "a lot" vibe (signed=2 at source 1.0) crosses THEME_THRESHOLD in one event.
    graduation.graduate_moods(s, [identity_key("song", "band")], 2.0, 1000.0,
                             source=rec_params.SOURCE_W_VIBE, source_label="vibe")
    rows = s.rec_signals.recent_graduations()
    assert rows, "a threshold crossing must log at least one graduation row"
    assert all(r["source"] == "vibe" and r["created_at"] == 1000.0 for r in rows)
    genre_axis = f"genre:{genre_map.family('Techno')}"
    genre_rows = [r for r in rows if r["axis"] == genre_axis]
    assert len(genre_rows) == 1
    assert genre_rows[0]["new_weight"] > 1.0          # the resulting permanent weight after the nudge
    assert genre_rows[0]["score"] >= rec_params.get_param(s, "theme_threshold")


def test_subthreshold_event_logs_nothing():
    s = _store_with_genre("v1", "song", "band", "Techno")
    # A single weak play (source 0.08, signed +1) does NOT cross the threshold, so nothing graduates.
    graduation.graduate_moods(s, [identity_key("song", "band")], 1.0, 1000.0,
                             source=rec_params.SOURCE_W_PLAY)
    assert s.rec_signals.recent_graduations() == []


def test_graduation_counts_aggregate_by_source():
    s = Store(":memory:")
    s.init_schema()
    t1 = s.tracks.upsert_track("v1", "song", "band", None, None); s.tracks.set_track_genre(t1, "Techno")
    t2 = s.tracks.upsert_track("v2", "tune", "act", None, None); s.tracks.set_track_genre(t2, "Folk")
    graduation.graduate_moods(s, [identity_key("song", "band")], 2.0, 1000.0,
                             source=rec_params.SOURCE_W_VIBE, source_label="vibe")
    graduation.graduate_moods(s, [identity_key("tune", "act")], 2.0, 1000.0,
                             source=rec_params.SOURCE_W_LIKE, source_label="like")
    counts = s.rec_signals.graduation_log_counts()
    assert set(counts) == {"vibe", "like"}
    assert counts["vibe"] >= 1 and counts["like"] >= 1
    assert sum(counts.values()) == len(s.rec_signals.recent_graduations())   # the counts partition the log
