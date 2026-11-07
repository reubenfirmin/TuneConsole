"""Group presentation and work limits over detached facts, without SQLite."""
from contextlib import contextmanager
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
from yt_playlist.controllers.group_retention import GroupRetention
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.group_documents import GroupFiles
from yt_playlist.core.profile_files import ProfileFiles
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def groups():
    repository = create_autospec(CompactTasteGroupsRepo, instance=True, spec_set=True)
    files = create_autospec(GroupFiles, instance=True, spec_set=True)
    active = []

    @contextmanager
    def retained():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    files.retained.side_effect = retained
    files.policy.return_value = {'algorithm': 'groups', 'version': 1}

    def document(row, kind):
        assert active, 'Files must stay retained until the complete read model is assembled'
        return row['document']

    files.read.side_effect = document
    return CompactTasteGroups(repository, files, create_autospec(ProfileFiles, instance=True, spec_set=True),
                              create_autospec(GroupRetention, instance=True, spec_set=True))


def test_head_uses_exact_sql_membership_and_parent_aliases(groups):
    definition = {'external_key': 'group', 'external_version': 'alias-2', 'parent_version': 'parent-2',
                  'members': {'recording': (.8, .9)}, 'created_at': 1,
                  'document': {'leaf': True, 'group_id': 'obsolete', 'version': 'obsolete'}}
    groups.repository.head.return_value = {
        'external_key': 'generation-2', 'input_fingerprint': 'input', 'created_at': 5, 'input_serial': 9,
        'recording_inputs': {'recording': 'digest'}, 'policy': 'policy-reference',
        'document': {'diagnostics': {}, 'recording_inputs': {'obsolete': 'old'}}, 'groups': [definition],
    }
    assert groups.head(members=True) == {
        'generation': 'generation-2', 'fingerprint': 'input', 'created_at': 5, 'serial': 9,
        'policy': '{"algorithm":"groups","version":1}',
        'payload': {'diagnostics': {}, 'recording_inputs': {'recording': 'digest'}},
        'groups': [{'leaf': True, 'group_id': 'group', 'version': 'alias-2',
                    'parent_version': 'parent-2', 'members': {'recording': (.8, .9)}}],
    }


def test_exact_historical_alias_is_looked_up_in_the_requested_generation(groups):
    groups.repository.definition.return_value = {
        'external_key': 'group', 'external_version': 'served-alias', 'parent_version': 'served-parent',
        'generation': 'historical', 'created_at': 4, 'document': {'leaf': True},
    }
    assert groups.definition('served-alias', generation='historical') == {
        'group_id': 'group', 'version': 'served-alias', 'parent_version': 'served-parent',
        'generation': 'historical', 'created_at': 4, 'leaf': True,
    }
    groups.repository.definition.assert_called_once_with('served-alias', generation='historical')


@pytest.mark.parametrize('method,args', [('head', ()), ('head_checkpoint', ()), ('definition', ('missing',)),
                                         ('learning_head', ())])
def test_absent_publications_do_not_attempt_file_reads(groups, method, args):
    getattr(groups.repository, method).return_value = None
    assert getattr(groups, method)(*args) is None
    groups.files.read.assert_not_called()
    groups.files.policy.assert_not_called()


@pytest.mark.parametrize('limit', [-1, True, 8193, '10'])
def test_invalid_lineage_budget_is_rejected_before_sql(groups, limit):
    with pytest.raises(ValueError, match='page size'):
        groups.successors('alias', limit=limit)
    groups.repository.successors.assert_not_called()


def test_lineage_overflow_is_not_silently_truncated_or_decoded(groups):
    groups.repository.successors.return_value = [{}, {}]
    with pytest.raises(WorkBudgetExceeded):
        groups.successors('alias', limit=1)
    groups.files.read.assert_not_called()


def test_learning_head_exposes_the_hierarchy_and_genres_without_diagnostic_payload(groups):
    groups.repository.learning_head.return_value = {'generation': 'generation', 'groups': [{
        'group_id': 'group', 'version': 'alias', 'parent_version': None, 'legacy': 0, 'leaf': 1,
        'document': {'genres': ['specific'], 'diagnostics': {'private': 'large graph'}},
    }]}
    assert groups.learning_head() == {'generation': 'generation', 'groups': [{
        'group_id': 'group', 'version': 'alias', 'parent_version': None, 'legacy': 0, 'leaf': 1,
        'genres': ['specific'],
    }]}


def test_unreadable_retained_content_is_not_reported_as_an_empty_group(groups):
    groups.repository.definition.return_value = {'document': {}}
    groups.files.read.side_effect = ValueError('Invalid musical group pack')
    with pytest.raises(ValueError, match='Invalid musical group pack'):
        groups.definition('alias')
