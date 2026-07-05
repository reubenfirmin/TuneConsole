"""#85 transient decay is wall-clock: an old event fades by age even with nothing newer,
and 50 rapid plays cannot rotate an event out of the window by rank."""
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import transient

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def _track(s, key, genre="techno"):
    t = s.upsert_track(f"v_{key}", key.split("|")[0], key.split("|")[1], "", 200)
    s.set_track_genre(t, genre)
    return t


def test_month_old_mood_tap_is_nearly_gone():
    s = _store()
    _track(s, "song|artist")
    now = 1000 * DAY
    s.record_mood(["song|artist"], +1, now - 30 * DAY)      # one month old, nothing since
    leans = transient.facet_leans(s, now)
    val = leans.get("genre:techno", 0.0)
    assert 0 <= val < 0.06                                   # 30d at 7d half-life ~ 0.05
    fresh = transient.facet_leans(s, now - 30 * DAY + 60)    # same event when it was a minute old
    fval = fresh.get("genre:techno", 0.0)
    assert fval > 0.9


def test_rapid_plays_do_not_erase_a_recent_like():
    # The old rank decay let 50 quick plays rotate a like to rank 50 (~0 weight). Wall-clock:
    # a like from an hour ago keeps ~full weight no matter how many plays follow it.
    s = _store()
    _track(s, "loved|artist", genre="jazz")
    now = 1000 * DAY
    s.record_like("loved|artist", now - 3600, provenance="action")  # a live thumbs-up (transient)
    for i in range(50):
        _track(s, f"p{i}|other", genre="techno")
        s.record_play_event(1, f"p{i}|other", None, now - 1800 + i * 30)
    leans = transient.facet_leans(s, now)
    jazz = leans.get("genre:jazz", 0.0)
    assert jazz > 0.4 * float(__import__("yt_playlist.rec.rec_params", fromlist=["x"]).get_param(s, "like_transient_w")) or jazz > 0.4


def test_staleness_factor_is_gone():
    assert not hasattr(transient, "staleness_factor")


def test_quiet_model_still_returns_empty_and_none():
    s = _store()
    assert transient.facet_leans(s, 1000.0) == {}
    assert transient.play_facet_leans(s, 1000.0) == {}
