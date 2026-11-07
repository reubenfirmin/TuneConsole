import json
from copy import deepcopy
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artist_state import ArtistState
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.repos.artist_state import ArtistStateRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def state():
    return ArtistState(create_autospec(ArtistStateRepo, instance=True, spec_set=True),
                       create_autospec(RetainedOriginals, instance=True, spec_set=True))


@pytest.mark.parametrize('old_time,new_time,winner', [(None, 2, 'new'), (3, 2, 'old'), (2, 2, 'new')])
def test_similarity_merge_retains_both_originals_and_prefers_latest_then_target(state, old_time, new_time, winner):
    rows = [dict(artist_id=7, artist='@artist:7', fetched_at=old_time, payload='old'),
            dict(artist_id=12, artist='@artist:12', fetched_at=new_time, payload='new')]
    original = deepcopy(rows)
    state.repository.reconciliation_inputs.return_value = {'caches': rows, 'edges': []}
    state.reconcile_artist(7, 12, source='youtube')
    retained = [json.loads(call.args[3]) for call in state.originals.retain.call_args_list]
    assert retained == original == rows
    actual = state.repository.apply_reconciliation.call_args.args[2]
    assert actual['artist_id'] == 12 and actual['artist'] == '@artist:12' and actual['payload'] == winner


def test_similarity_rebinds_both_ends_without_dropping_self_edges(state):
    state.repository.reconciliation_inputs.return_value = {'caches': [], 'edges': [
        dict(source_artist_id=7, target_artist_id=12, weight=.4),
        dict(source_artist_id=3, target_artist_id=7, weight=.8)]}
    state.reconcile_artist(7, 12, source='youtube')
    state.repository.apply_reconciliation.assert_called_once_with(7, 12, None, [(12, 12, .4), (3, 12, .8)])


def test_failed_original_retention_prevents_similarity_merge(state):
    state.repository.reconciliation_inputs.return_value = {'caches': [
        dict(artist_id=7, artist='@artist:7', fetched_at=1)], 'edges': []}
    state.originals.retain.side_effect = OSError('write failed')
    with pytest.raises(OSError, match='write failed'):
        state.reconcile_artist(7, 12, source='youtube')
    state.repository.apply_reconciliation.assert_not_called()
