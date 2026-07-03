"""#85 mood events age out: the table is bounded by TIME as well as the legacy 200-row cap."""
from yt_playlist.core.store import Store

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_prune_before_drops_old_events_only():
    s = _store()
    s.record_mood(["a|x"], +1, 100 * DAY)
    s.record_mood(["b|y"], -1, 130 * DAY, prune_before=101 * DAY)
    events = s.recent_mood_events()
    assert len(events) == 1 and events[0][2] == ["b|y"]


def test_no_prune_when_unset():
    s = _store()
    s.record_mood(["a|x"], +1, 100 * DAY)
    s.record_mood(["b|y"], -1, 130 * DAY)
    assert len(s.recent_mood_events()) == 2
