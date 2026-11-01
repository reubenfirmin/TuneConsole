"""Run the split/rejoin evidence contract through the current compact schema."""
import pytest

from tests import test_group_learning as contract
from yt_playlist.core.db_revisions import Revisions


@pytest.fixture
def compact_store(store):
    store._snapshot_revisions = Revisions(store.conn)
    assert store._compact_group_files
    return store


@pytest.mark.parametrize('transition', ['continuation', 'rejoin'])
@pytest.mark.parametrize('source', ['parent', 'child'])
@pytest.mark.parametrize('storage', ['raw', 'acknowledged', 'legacy-checkpoint',
                                     'acknowledged-released', 'legacy-released'])
def test_frozen_support_survives_compact_publication_retention_and_restart_contexts(
        compact_store, monkeypatch, transition, source, storage):
    # This exercises real discovery/preparation, capture, checkpoints, learning,
    # parent support, immutable file bytes and retention. No group lookup or
    # generation mapping is replaced by a mock.
    contract.test_frozen_broad_support_survives_child_rejoin(
        compact_store, monkeypatch, transition, source, storage)
