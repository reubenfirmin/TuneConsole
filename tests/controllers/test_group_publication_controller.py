"""Group file/SQL ordering and retry decisions without a database fixture."""
from contextlib import contextmanager
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.group_publication import GroupPublication
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
    active, events = [], []

    @contextmanager
    def retained():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    files.retained.side_effect = retained
    files.write_policy.return_value = ('policy.json', 'policy-digest', 10, 0)

    def write(documents):
        assert active
        values = list(documents)
        events.extend(('file', kind) for kind, _value in values)
        return [(kind + '.json', 'digest', 10, 0) for kind, _value in values]

    files.write.side_effect = write
    repo.recording_ids.return_value = {'one': 1}
    repo.claim_generation.return_value = 10, 11, 'owner', False
    repo.definition_candidates.return_value = [(0, 12)]
    repo.group_state.return_value = {'version_id': 13, 'group_id': 12, 'sealed': False,
                                     'members': 0, 'representatives': 0}
    repo.lineage_prefix.return_value = 0
    repo.lineage_facts.return_value = {}
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

    repo.install_definitions.side_effect = installing
    prepared = {'generation': 'next', 'fingerprint': 'fingerprint', 'at': 3, 'serial': 3,
                'policy': canonical({'version': 1}).decode(),
                'payload': canonical({'recording_inputs': {'one': 'a' * 64}}).decode(),
                'groups': [{'group_id': 'group', 'version': 'alias', 'parent_version': None,
                            'payload': canonical(definition).decode(), 'members': [('one', .9, .8)]}],
                'unknown': [], 'edges': [], 'lineage': []}
    return GroupPublication(repo, files), prepared, events, active


def test_files_are_published_before_their_sql_references_and_head_is_last(publication):
    service, prepared, events, active = publication
    service.publish(prepared)
    assert events.index(('file', 'definition')) < events.index(('sql', 'definition'))
    service.repository.install_members.assert_called_once_with(
        10, 'owner', 13, [('one', .9, .8)], {'one': 1}, {'one': 'a' * 64})
    service.repository.link_group.assert_called_once_with(10, 'owner', 12, 13, 0, 'alias')
    assert service.repository.method_calls[-2:] == [
        ('mark_ready', (10, 'owner', 1), {}), ('finish_staged', ('next', 'owner'), {})]
    assert not active


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 1.1, True])
def test_invalid_graph_scores_never_claim_a_build_or_publish_files(publication, bad):
    service, prepared, _events, _active = publication
    prepared['edges'] = [('one', 'missing', bad, .5)]
    with pytest.raises(ValueError, match='scores must be finite'):
        service.stage(prepared)
    service.repository.claim_generation.assert_not_called()
    service.files.write.assert_not_called()


def test_failed_generation_file_does_not_create_a_sql_generation(publication):
    service, prepared, _events, active = publication
    service.files.write.side_effect = OSError('disk full')
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
    service.repository.install_inputs.assert_not_called()
    service.repository.finish_staged.assert_not_called()
    assert not active


def test_interrupted_member_installation_is_retryable_without_advancing_the_head(publication):
    service, prepared, _events, active = publication
    service.repository.install_members.side_effect = WorkClosing('stop')
    with pytest.raises(WorkClosing):
        service.publish(prepared)
    service.repository.link_group.assert_not_called()
    service.repository.finish_staged.assert_not_called()
    assert not active
    service.repository.install_members.side_effect = None
    service.repository.group_state.return_value['members'] = 1
    service.publish(prepared)
    assert service.repository.install_members.call_count == 1
    service.repository.finish_staged.assert_called_once_with('next', 'owner')


def test_digest_match_with_different_actual_members_cannot_advance_the_head(publication):
    service, prepared, _events, _active = publication
    service.repository.version_facts.return_value['members'][0] = (1, .1, .8, 'a' * 64)
    with pytest.raises(ValueError, match='digest collision'):
        service.publish(prepared)
    service.repository.link_group.assert_not_called()
    service.repository.finish_staged.assert_not_called()


def test_reused_sealed_content_is_verified_before_linking_without_resume_counts(publication):
    service, prepared, _events, _active = publication
    service.repository.group_state.return_value = {
        'version_id': 13, 'group_id': 12, 'sealed': True,
    }
    service.repository.version_facts.return_value['members'][0] = (1, .1, .8, 'a' * 64)
    with pytest.raises(ValueError, match='digest collision'):
        service.publish(prepared)
    service.repository.install_members.assert_not_called()
    service.repository.install_representatives.assert_not_called()
    service.repository.link_group.assert_not_called()
    service.repository.finish_staged.assert_not_called()


def test_ready_build_only_needs_its_final_publication(publication):
    service, prepared, _events, _active = publication
    service.repository.claim_generation.return_value = 10, 11, 'owner', True
    service.publish(prepared)
    service.repository.install_inputs.assert_not_called()
    service.repository.install_definitions.assert_not_called()
    service.repository.finish_staged.assert_called_once_with('next', 'owner')


def test_same_version_cannot_silently_reuse_changed_temporal_evidence(publication):
    service, prepared, _events, _active = publication
    value = {'old_share': .5, 'new_share': .5, 'overlap': 1, 'compatible': True}
    prepared['lineage'] = [('before', 'alias', 'continuation', canonical(value).decode())]
    previous = {'relation': 'continuation'}
    service.repository.lineage_facts.return_value = {('before', 'alias'): previous}
    original_read = service.files.read.return_value
    service.files.read.side_effect = lambda row, kind: ({**value, 'overlap': 2} if kind == 'lineage' else original_read)
    with pytest.raises(ValueError, match='Temporal musical group evidence changed'):
        service.publish(prepared)
    service.repository.install_lineage.assert_not_called()
    service.repository.finish_staged.assert_not_called()
