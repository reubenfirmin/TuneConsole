"""HistoryRepo reads current dated listening observations."""


def test_dated_observations_roundtrip_and_recent_keys(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.history.record_history_plays(iid, 100*86400+43200, ["a", "b"], source_timezone="UTC")
    store.history.record_history_plays(iid, 101*86400+43200, ["b", "c"], source_timezone="UTC")
    assert store.history.get_recent_history_keys(since_ts=0) == {"a", "b", "c"}
    assert store.history.get_recent_history_keys(since_ts=101*86400) == {"b", "c"}


def test_store_binding(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.history.record_history_plays(iid, 1000.0, ["x"], source_timezone="UTC")
    assert store.history.get_recent_history_keys(0) == {"x"}
