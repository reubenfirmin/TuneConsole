"""#85 timestamped transient-event sources: real play_events timestamps with day-model fallback."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def test_plays_with_ts_prefers_real_timestamps():
    s = _store()
    # day-model row at noon of day 100; live play_event later the same day with a REAL timestamp
    s.record_history_plays(1, 100 * 86400 + 50000, ["song|artist"])
    s.record_play_event(1, "song|artist", "v1", 100 * 86400 + 61000)
    out = s.recent_plays_with_ts()
    assert out == [("song|artist", 100 * 86400 + 61000)]


def test_plays_with_ts_day_model_fallback_and_order():
    s = _store()
    s.record_history_plays(1, 100 * 86400 + 50000, ["old|a"])      # noon bucket only
    s.record_play_event(1, "new|b", "v2", 101 * 86400 + 3600.0)
    out = s.recent_plays_with_ts()
    assert [k for k, _ in out] == ["new|b", "old|a"]
    assert out[1][1] == 100 * 86400 + 43200                        # noon-of-day fallback ts


def test_plays_with_ts_limit():
    s = _store()
    for i in range(5):
        s.record_play_event(1, f"k{i}|a", None, 1000.0 + i * 4000)
    assert len(s.recent_plays_with_ts(limit=2)) == 2


def test_liked_and_disliked_with_ts():
    s = _store()
    s.record_like("liked|a", 5000.0)
    s.record_dislike("bad|b", 99999.0, 6000.0)
    assert s.recent_liked_with_ts() == [("liked|a", 5000.0)]
    assert s.disliked_with_ts() == [("bad|b", 6000.0)]
