import pytest
from tests.mode_fixtures import seed_modes


@pytest.fixture
def store(store):
    seed_modes(store,1,2)
    return store


def test_impressions_persist_ranker_and_accept_legacy_two_tuple(store):
    store.modes.log_impressions(1, [("wheelhouse", 1, "ppr"), ("explore", 2, "cosine")], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)
    rows = store.conn.execute('SELECT ranker FROM rec_mode_impressions ORDER BY epoch, lane').fetchall()
    assert [row['ranker'] for row in rows] == ['cosine', 'ppr', None]


def test_picks_persist_ranker_and_accept_legacy_pick(store):
    store.modes.log_pick(10, 1, 100.0, ranker="ppr")
    store.modes.log_pick(11, 2, 200.0)
    rows = store.conn.execute('SELECT membership_ref_id, ranker FROM rec_mode_picks ORDER BY membership_ref_id').fetchall()
    assert [(row['membership_ref_id'], row['ranker']) for row in rows] == [(10, 'ppr'), (11, None)]
