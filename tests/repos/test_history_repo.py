"""HistoryRepo reads current dated listening observations."""


def test_dated_observations_roundtrip_with_latest_timestamps(store):
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.history.record_history_plays(iid, 100*86400+43200, ["a", "b"], source_timezone="UTC")
    store.history.record_history_plays(iid, 101*86400+43200, ["b", "c"], source_timezone="UTC")
    assert dict(store.history.recent_plays_with_ts()) == {
        "a": 100*86400+43200, "b": 101*86400+43200, "c": 101*86400+43200,
    }
    assert {key for key, _ in store.history.recent_plays_with_ts(limit=2)} == {"b", "c"}
    assert store.history.history_keys_between(101*86400, 102*86400) == {"b", "c"}
