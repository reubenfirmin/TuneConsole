"""#87 lane impressions: the data the future lane bandit needs, logged from home renders."""
from yt_playlist.core.store import Store

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_record_and_count():
    s = _store()
    s.record_lane_impressions([("neighbourhood", "a|x"), ("deep_cut", "b|y"),
                               ("neighbourhood", "c|z")], 1000.0)
    assert s.lane_impression_counts() == {"neighbourhood": 2, "deep_cut": 1}


def test_prune_on_write():
    s = _store()
    s.record_lane_impressions([("comfort", "old|x")], 100 * DAY)
    s.record_lane_impressions([("comfort", "new|y")], 200 * DAY, prune_before=150 * DAY)
    assert s.lane_impression_counts() == {"comfort": 1}


def test_since_filter():
    s = _store()
    s.record_lane_impressions([("comfort", "a|x")], 100.0)
    s.record_lane_impressions([("comfort", "b|y")], 900.0)
    assert s.lane_impression_counts(since=500.0) == {"comfort": 1}


def test_empty_items_is_noop():
    s = _store()
    s.record_lane_impressions([], 1000.0)
    assert s.lane_impression_counts() == {}
