import pytest
from tests.mode_fixtures import seed_modes


@pytest.fixture
def store(store):
    seed_modes(store,1,2)
    return store


def test_impressions_idempotent_and_counted(store):
    store.modes.log_impressions(5, [("wheelhouse", 1), ("explore", 2)], now=100.0)
    store.modes.log_impressions(5, [("wheelhouse", 1), ("explore", 2)], now=150.0)  # same epoch, ignored
    store.modes.log_impressions(6, [("wheelhouse", 1)], now=200.0)
    assert store.modes.impression_counts() == {1: 2, 2: 1}


def test_impression_counts_since(store):
    store.modes.log_impressions(1, [("wheelhouse", 1)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=300.0)
    assert store.modes.impression_counts(since=200.0) == {1: 1}


def test_picks_idempotent_and_counted_within_window(store):
    store.modes.log_pick(membership_ref_id=10, mode_id=1, now=100.0)
    store.modes.log_pick(membership_ref_id=10, mode_id=1, now=150.0)   # same membership, ignored
    store.modes.log_pick(membership_ref_id=11, mode_id=2, now=200.0)
    assert store.modes.pick_counts() == {1: 1, 2: 1}
    assert store.modes.pick_counts(since=150.0) == {2: 1}
    assert store.modes.pick_counts(before=200.0) == {1: 1}
    rows = store.conn.execute(
        'SELECT membership_ref_id, mode_id, created_at FROM rec_mode_picks ORDER BY membership_ref_id')
    assert [tuple(row) for row in rows] == [(10, 1, 100.0), (11, 2, 200.0)]
