"""DAO suite for HistoryRepo (listening-history snapshots)."""


def test_snapshot_roundtrip_and_recent_keys(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.history.add_history_snapshot(iid, taken_at=1000.0, item_keys=["a", "b"])
    store.history.add_history_snapshot(iid, taken_at=2000.0, item_keys=["b", "c"])
    assert store.history.get_recent_history_keys(since_ts=0) == {"a", "b", "c"}
    assert store.history.get_recent_history_keys(since_ts=1500.0) == {"b", "c"}   # only the 2nd snapshot


def test_facade_delegates(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.add_history_snapshot(iid, 1000.0, ["x"])                                # legacy store.x() call site
    assert store.get_recent_history_keys(0) == {"x"}
