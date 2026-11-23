from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artist_state import ArtistState
from yt_playlist.repos.artist_state import ArtistStateRepo

@pytest.fixture
def state():
    return ArtistState(create_autospec(ArtistStateRepo, instance=True, spec_set=True))


@pytest.mark.parametrize('old_time,new_time,winner', [(None, 2, 'new'), (3, 2, 'old'), (2, 2, 'new')])
def test_similarity_merge_prefers_latest_then_target(store, old_time, new_time, winner):
    old = store.artists.ensure_artist('Old')
    target = store.artists.ensure_artist('Target')
    with store.conn:
        store.conn.executemany('UPDATE artists SET lastfm_similar_json=?,lastfm_fetched_at=? WHERE id=?',
                               [('"old"', old_time, old), ('"new"', new_time, target)])
    store.artist_state.reconcile_artist(old, target, source='youtube')
    query = 'SELECT lastfm_similar_json,lastfm_fetched_at FROM artists WHERE id=?'
    assert tuple(store.conn.execute(query, (target,)).fetchone()) == (
        f'"{winner}"', old_time if winner == 'old' else new_time)
    assert tuple(store.conn.execute(query, (old,)).fetchone()) == (None, None)


@pytest.mark.usefixtures('no_database')
def test_similarity_rebinds_both_ends_without_dropping_self_edges(state):
    state.repository.reconciliation_inputs.return_value = [
        dict(source_artist_id=7, target_artist_id=12, weight=.4),
        dict(source_artist_id=3, target_artist_id=7, weight=.8)]
    state.reconcile_artist(7, 12, source='youtube')
    state.repository.apply_reconciliation.assert_called_once_with(7, 12, [(12, 12, .4), (3, 12, .8)])
