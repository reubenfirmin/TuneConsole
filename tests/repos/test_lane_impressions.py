"""#87 lane impressions: the data the future lane bandit needs, logged from home renders."""
from yt_playlist.core.store import Store

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def _record(s, items, now, prune_before=None):
    s.rec_ledger.record_served_impressions(str(now), "home", [
        {"lane": lane, "key": key, "rank": rank} for rank, (lane, key) in enumerate(items, 1)
    ], now, prune_before=prune_before)


def test_record_and_count():
    s = _store()
    _record(s, [("neighbourhood", "a|x"), ("deep_cut", "b|y"),
                ("neighbourhood", "c|z")], 1000.0)
    assert s.rec_ledger.lane_impression_counts() == {"neighbourhood": 2, "deep_cut": 1}


def test_prune_on_write():
    s = _store()
    _record(s, [("comfort", "old|x")], 100 * DAY)
    _record(s, [("comfort", "new|y")], 200 * DAY, prune_before=150 * DAY)
    assert s.rec_ledger.lane_impression_counts() == {"comfort": 1}


def test_since_filter():
    s = _store()
    _record(s, [("comfort", "a|x")], 100.0)
    _record(s, [("comfort", "b|y")], 900.0)
    assert s.rec_ledger.lane_impression_counts(since=500.0) == {"comfort": 1}


def test_empty_items_is_noop():
    s = _store()
    _record(s, [], 1000.0)
    assert s.rec_ledger.lane_impression_counts() == {}
