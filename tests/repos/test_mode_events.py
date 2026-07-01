import pytest
from yt_playlist.core.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_impressions_idempotent_and_counted(store):
    store.modes.log_impressions(5, [("wheelhouse", 1), ("explore", 2)], now=100.0)
    store.modes.log_impressions(5, [("wheelhouse", 1), ("explore", 2)], now=150.0)  # same epoch, ignored
    store.modes.log_impressions(6, [("wheelhouse", 1)], now=200.0)
    assert store.modes.impression_counts() == {1: 2, 2: 1}


def test_impression_counts_since(store):
    store.modes.log_impressions(1, [("wheelhouse", 1)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=300.0)
    assert store.modes.impression_counts(since=200.0) == {1: 1}


def test_picks_idempotent_and_rows(store):
    store.modes.log_pick(playlist_id=10, mode_id=1, now=100.0)
    store.modes.log_pick(playlist_id=10, mode_id=1, now=150.0)   # same playlist, ignored
    store.modes.log_pick(playlist_id=11, mode_id=2, now=200.0)
    rows = sorted(store.modes.pick_rows())
    assert rows == [(10, 1), (11, 2)]
    assert sorted(store.modes.pick_rows(since=150.0)) == [(11, 2)]
