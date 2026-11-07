"""Evidence retention rules and guarded publication over mocked SQL facts."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.group_retention import GroupRetention
from yt_playlist.core import group_retention_policy as policy
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.repos.group_retention_queries import GroupRetentionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def repository():
    repo = create_autospec(GroupRetentionRepo, instance=True, spec_set=True)
    repo.retention_state.return_value = {'complete': True, 'walk': False, 'revision': None,
                                         'recommendations': True, 'checkpoint': True}
    repo.root_references.return_value = ({2}, {'current'})
    repo.published_successors.return_value = []
    repo.alias_origins.return_value = {2}
    repo.lineage_origins.return_value = set()
    repo.published_edges.return_value = []
    repo.published_generations.return_value = [3, 2, 1]
    repo.alias_selected_elsewhere.return_value = False
    return repo


def test_backfill_fence_prevents_all_retirement_queries(repository):
    repository.retention_state.return_value['complete'] = False
    assert policy.plan(repository, batch=128) is None
    repository.root_references.assert_not_called()
    repository.advance_walk.assert_not_called()
    repository.published_generations.assert_not_called()


def test_roots_expand_actual_contexts_but_not_an_unfinished_build():
    assert policy.roots(recommendations=True, checkpoint=True) == [
        ('head', True), ('evidence', True), ('build', False), ('build_parent', True),
        ('recommendation', True), ('checkpoint', True)]
    assert policy.roots(recommendations=False, checkpoint=False)[-1] == ('build_parent', True)


def test_closure_follows_outgoing_paths_and_keeps_original_contexts(repository):
    repository.published_successors.side_effect = lambda alias, _limit: {'current': ['later'], 'later': ['current']}[alias]
    repository.alias_origins.return_value = {2, 4}
    repository.lineage_origins.return_value = {5}
    generations, edges = policy.closure(repository, [('head', True)])
    assert generations == {2, 4, 5}
    assert edges == {('current', 'later'), ('later', 'current')}
    assert repository.published_successors.call_count == 2
    repository.alias_origins.assert_called_once_with({'current', 'later'})


def test_bounded_closure_does_not_silently_drop_reachable_history(repository, monkeypatch):
    monkeypatch.setattr(policy, 'MAX_REFERENCES', 1)
    repository.published_successors.return_value = ['later']
    with pytest.raises(WorkBudgetExceeded, match='alias budget'):
        policy.closure(repository, [('head', True)])


def test_edges_retire_before_publications_and_mapping_rows_obey_the_batch(repository):
    repository.published_edges.return_value = [('old', 'later'), ('other', 'later')]
    repository.edge_selections.return_value = [(1, 'old', 'later')]
    assert policy.plan(repository, batch=1) == {'edges': [(1, 'old', 'later')]}
    repository.edge_selections.assert_called_once_with([('old', 'later')], 1)
    repository.published_generations.assert_not_called()


def test_original_alias_context_stays_while_another_publication_selects_it(repository):
    repository.alias_selected_elsewhere.side_effect = lambda generation: generation == 3
    assert policy.plan(repository, batch=128) == {'generation': 1}


def test_no_unreferenced_publication_means_no_retirement(repository):
    repository.published_generations.return_value = [2]
    assert policy.plan(repository, batch=128) is None
    repository.alias_selected_elsewhere.assert_not_called()


@pytest.mark.parametrize('ready', [False, True])
def test_durable_walk_carries_its_root_revision(repository, ready):
    repository.retention_state.return_value.update(walk=True, revision=9)
    commands = [('bounded SQL cursor update', ())]
    repository.advance_walk.return_value = None if ready else commands
    repository.walk_expired.return_value = {'generation': 1}
    assert policy.plan(repository, batch=128) == (
        {'generation': 1, 'revision': 9} if ready else {'walk': commands, 'revision': 9})
    repository.root_references.assert_not_called()


@pytest.mark.parametrize('current', [False, True])
def test_controller_releases_its_reader_then_applies_only_a_current_plan(repository, current):
    target = create_autospec(GroupRetentionRepo, instance=True, spec_set=True)
    released = Mock()

    def publish(callback):
        released.assert_called_once()
        if current:
            callback()
        return current

    snapshot = SimpleNamespace(store=SimpleNamespace(group_retention=repository),
                               release_reader=released, publish=Mock(side_effect=publish))

    @contextmanager
    def snapshots():
        yield snapshot

    assert GroupRetention(target, snapshots).step() is current
    if current:
        target.apply.assert_called_once_with({'generation': 3})
    else:
        target.apply.assert_not_called()
