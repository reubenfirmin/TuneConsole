import pytest
from tests.mode_fixtures import seed_modes


@pytest.fixture
def store(store):
    seed_modes(store,1,2)
    return store


def test_impressions_persist_ranker_and_accept_legacy_two_tuple(store):
    store.modes.log_impressions(1, [("wheelhouse", 1, "ppr"), ("explore", 2, "cosine")], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)
    rows = store.conn.execute('SELECT ranker FROM taste_mode_offers ORDER BY epoch, lane').fetchall()
    assert [row['ranker'] for row in rows] == ['cosine', 'ppr', None]


def test_picks_persist_ranker_and_accept_legacy_pick(store):
    store.modes.log_pick(10, 1, 100.0, ranker="ppr")
    store.modes.log_pick(11, 2, 200.0)
    rows = store.conn.execute('SELECT id,picked_ranker FROM playlists WHERE mode_picked_at IS NOT NULL ORDER BY id').fetchall()
    assert [(row['id'], row['picked_ranker']) for row in rows] == [(10, 'ppr'), (11, None)]
