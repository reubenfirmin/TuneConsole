"""Group file/SQL ordering and retry decisions without a database fixture."""
from contextlib import contextmanager
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.group_publication import GroupPublication
from yt_playlist.controllers.policies import Policies
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.group_documents import GroupFiles
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos.group_publication import BuildUnavailable

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def publication():
    repo = create_autospec(CompactTasteGroupsRepo, instance=True, spec_set=True)
    files = create_autospec(GroupFiles, instance=True, spec_set=True)
    policies = create_autospec(Policies, instance=True, spec_set=True)
    active, events = [], []

    @contextmanager
    def retained():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    files.retained.side_effect = retained
    def publish(documents):
        assert active
        references = []
        for kind, _value in documents:
            events.append(('file', kind))
            references.append((kind + '.json', 'digest', 10, 0))
        return references

    files.publish.side_effect = publish
    repo.recording_ids.return_value = {'one': 1}
    repo.conflicting_build.return_value = None

    def claim(*_args):
        assert active
        events.append(('sql', 'generation'))
        return 10, 'owner', False

    repo.claim_generation.side_effect = claim
    repo.generation_reference.return_value = None
    repo.stages.side_effect = lambda *_args: {}
    definition = {'leaf': True, 'membership_known': True, 'size': 1, 'rep_keys': ['one']}
    files.read.return_value = definition
    files.policy.return_value = {'version': 1}
    repo.version_facts.return_value = {
        'version': {'external_key': 'group', 'parent_key': None}, 'policy': 'policy-reference',
        'members': [(1, .9, .8, 'a' * 64)], 'unresolved': [],
    }

    def installing(*_args, **_kwargs):
        assert active
        events.append(('sql', 'definition'))
        return {'group': {'id': 13, 'external_version': 'alias', 'complete': False, 'member_count': 0}}

    repo.install_definitions.side_effect = installing
    prepared = {'generation': 'next', 'fingerprint': 'fingerprint', 'at': 3, 'serial': 3,
                'policy': canonical({'version': 1}).decode(),
                'payload': canonical({'recording_inputs': {'one': 'a' * 64}}).decode(),
                'groups': [{'group_id': 'group', 'version': 'alias', 'parent_version': None,
                            'payload': canonical(definition).decode(), 'members': [('one', .9, .8)]}],
                'unknown': [], 'edges': []}
    return GroupPublication(repo, files, policies), prepared, events, active


def test_files_are_published_before_their_sql_references_and_head_is_last(publication):
    service, prepared, events, active = publication
    service.publish(prepared)
    assert events.index(('file', 'generation')) < events.index(('sql', 'generation'))
    assert events.index(('file', 'definition')) < events.index(('sql', 'definition'))
    service.repository.install_members.assert_called_once_with(
        10, 'owner', 13, [('one', .9, .8)], {'one': 1}, {'one': 'a' * 64})
    assert service.repository.method_calls[-3:] == [
        ('seal', (10, 'owner', 13), {}), ('mark_ready', (10, 'owner', 1), {}),
        ('finish_staged', ('next', 'owner'), {})]
    assert not active


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 1.1, True])
def test_invalid_graph_scores_never_claim_a_build_or_publish_files(publication, bad):
    service, prepared, _events, _active = publication
    prepared['edges'] = [('one', 'missing', bad, .5)]
    with pytest.raises(ValueError, match='scores must be finite'):
        service.stage(prepared)
    service.repository.claim_generation.assert_not_called()
    service.files.publish.assert_not_called()


def test_failed_generation_file_does_not_create_a_sql_generation(publication):
    service, prepared, _events, active = publication
    service.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        service.publish(prepared)
    service.repository.claim_generation.assert_not_called()
    service.repository.finish_staged.assert_not_called()
    assert not active


def test_live_foreign_lease_keeps_the_previous_head(publication):
    service, prepared, _events, active = publication
    service.repository.claim_generation.side_effect = BuildUnavailable('another build')
    with pytest.raises(BuildUnavailable):
        service.publish(prepared)
    service.repository.install_identities.assert_not_called()
    service.repository.finish_staged.assert_not_called()
    assert not active


def test_interrupted_member_installation_is_retryable_without_advancing_the_head(publication):
    service, prepared, _events, active = publication
    service.repository.install_members.side_effect = WorkClosing('stop')
    with pytest.raises(WorkClosing):
        service.publish(prepared)
    service.repository.seal.assert_not_called()
    service.repository.finish_staged.assert_not_called()
    assert not active
    service.repository.install_members.side_effect = None
    service.repository.stages.side_effect = None
    service.repository.stages.return_value = {
        'group': {'id': 13, 'external_version': 'alias', 'complete': False, 'member_count': 1},
    }
    service.publish(prepared)
    assert service.repository.install_members.call_count == 1
    assert service.repository.install_definitions.call_count == 1
    service.repository.seal.assert_called_once_with(10, 'owner', 13)
    service.repository.finish_staged.assert_called_once_with('next', 'owner')


def test_digest_match_with_different_actual_members_cannot_advance_the_head(publication):
    service, prepared, _events, _active = publication
    service.repository.version_facts.return_value['members'][0] = (1, .1, .8, 'a' * 64)
    with pytest.raises(ValueError, match='digest collision'):
        service.publish(prepared)
    service.repository.seal.assert_not_called()
    service.repository.mark_ready.assert_not_called()
    service.repository.finish_staged.assert_not_called()


def test_resumed_sealed_group_needs_no_member_counts_or_rewrites(publication):
    service, prepared, _events, _active = publication
    service.repository.stages.side_effect = None
    service.repository.stages.return_value = {
        'group': {'id': 13, 'external_version': 'alias', 'complete': True},
    }
    service.publish(prepared)
    service.repository.install_definitions.assert_not_called()
    service.repository.install_members.assert_not_called()
    service.repository.version_facts.assert_not_called()
    service.repository.seal.assert_not_called()
    service.repository.mark_ready.assert_called_once_with(10, 'owner', 1)
    service.repository.finish_staged.assert_called_once_with('next', 'owner')


def test_ready_build_only_needs_its_final_publication(publication):
    service, prepared, _events, _active = publication
    service.repository.claim_generation.side_effect = None
    service.repository.claim_generation.return_value = 10, 'owner', True
    service.repository.generation_reference.return_value = ('generation.json', 'digest', 10, 0)
    service.publish(prepared)
    service.files.publish.assert_not_called()
    service.repository.install_identities.assert_not_called()
    service.repository.install_definitions.assert_not_called()
    service.repository.finish_staged.assert_called_once_with('next', 'owner')


def test_resumed_version_cannot_silently_reuse_changed_recording_evidence(publication):
    service, prepared, _events, _active = publication
    service.repository.stages.side_effect = None
    service.repository.stages.return_value = {
        'group': {'id': 13, 'external_version': 'alias', 'complete': False, 'member_count': 1},
    }
    prepared['payload'] = canonical({'recording_inputs': {'one': 'b' * 64}}).decode()
    with pytest.raises(ValueError, match='digest collision'):
        service.publish(prepared)
    service.repository.install_members.assert_not_called()
    service.repository.seal.assert_not_called()
    service.repository.mark_ready.assert_not_called()
    service.repository.finish_staged.assert_not_called()
