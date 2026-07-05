"""#83 adaptive holdout: a 6-day library gets a 2-day holdout instead of a silent None at 30."""
from yt_playlist.core.store import Store
from yt_playlist.rec.eval_recs import best_holdout

DAY = 86400.0


def _store_with_span(days):
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    s.record_history_plays(1, 100 * DAY + 50000, ["a|x"])
    if days:
        s.record_history_plays(1, (100 + days) * DAY + 50000, ["b|y"])
    return s


def test_thin_history_gets_small_holdout():
    assert best_holdout(_store_with_span(6)) == 2


def test_deep_history_caps_at_30():
    assert best_holdout(_store_with_span(365)) == 30


def test_empty_history_floors_at_1():
    s = Store(":memory:"); s.init_schema()
    assert best_holdout(s) == 1
