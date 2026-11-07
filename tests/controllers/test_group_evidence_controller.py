"""Group evidence coordination with mocked SQL and file owners."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.group_evidence import GroupEvidence
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.core.group_evidence_files import GroupEvidenceFiles
from yt_playlist.core.recommendation_outputs import CollectionProgress
from yt_playlist.repos.group_evidence import GroupEvidenceRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def evidence():
    repo = create_autospec(GroupEvidenceRepo, instance=True, spec_set=True)
    ledger = create_autospec(EvidenceLedger, instance=True, spec_set=True)
    files = create_autospec(GroupEvidenceFiles, instance=True, spec_set=True)
    repo.has_owner.return_value = False
    active = []

    @contextmanager
    def admission():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    service = GroupEvidence(repo, lambda: ledger, admission, files, admission, enabled=True, recommendations=True)
    return service, ledger, active


def context():
    return {'group_generation': 'generation', 'musical_groups': [{'group_id': 'group', 'version': 'alias'}]}


def test_retained_owner_is_prepared_before_sql(evidence):
    service, _ledger, _active = evidence
    service.retain('pending', 'reader', context(), 10, strict=True, recommendations=[7])
    owner = service.repository.retain.call_args.args[0]
    assert (owner.kind, owner.identity, owner.generation, owner.versions) == (
        'pending', 'reader', 'generation', (('alias', 'group'),))
    assert service.repository.retain.call_args.kwargs == {'strict': True, 'recommendations': [7]}


def test_invalid_references_are_rejected_before_sql(evidence):
    service, _ledger, _active = evidence
    with pytest.raises(ValueError, match='Invalid served musical group generation'):
        service.retain('pending', 'reader', {'group_generation': 7}, 10, strict=True, recommendations=[])
    service.repository.retain.assert_not_called()


def test_prepared_context_is_resolved_from_the_ledger_before_sql(evidence):
    service, ledger, _active = evidence
    ledger.get.return_value = context()
    service.replace_prepared('home_prepared', {'context': {'artifact': 'saved-context'}}, 10)
    kind, surface, operations, generations = service.repository.replace_prepared.call_args.args
    assert (kind, surface, generations) == ('prepared', 'home_prepared', [])
    assert operations[0][1].generation == 'generation'
    ledger.get.assert_called_once_with('artifact', 'saved-context')
    assert all(operation[1] is None for operation in operations[1:])


def test_already_resolved_historical_context_does_not_read_files(evidence):
    service, ledger, _active = evidence
    service.replace_prepared('home_prepared', {'context': {'artifact': 'old'}}, 10,
                             historical=True, resolved_contexts={'context': context()})
    ledger.get.assert_not_called()
    assert service.repository.replace_prepared.call_args.args[2][0][1].generation == 'generation'


def test_missing_historical_context_is_explicitly_unresolved(evidence):
    service, _ledger, _active = evidence
    service.replace_prepared('home_prepared', {'context': {'artifact': 'missing'}}, 10,
                             historical=True, resolved_contexts={})
    assert service.repository.replace_prepared.call_args.args[2][0] == (
        'proposal:home_prepared:context', None, ('missing', 'missing_context', 10), [])


def test_existing_historical_owner_is_never_reinterpreted(evidence):
    service, ledger, _active = evidence
    service.repository.has_owner.return_value = True
    service.replace_prepared('home_prepared', {'context': {'artifact': 'missing'}}, 10, historical=True)
    ledger.get.assert_not_called()
    assert service.repository.replace_prepared.call_args.args[2] == []


def test_choice_contexts_do_not_create_group_dependencies(evidence):
    service, ledger, _active = evidence
    service.replace_prepared('home_prepared', {'choice_context': context(), 'album_choice_context': context()}, 10)
    ledger.get.assert_not_called()
    assert all(owner is None and unknown is None for _identity, owner, unknown, _refs in
               service.repository.replace_prepared.call_args.args[2])


def test_recommendation_only_context_keeps_its_explicit_references(evidence):
    service, _ledger, _active = evidence
    service.replace_prepared('home_prepared', {'context': {'recommendation_generations': [7]}}, 10)
    assert service.repository.replace_prepared.call_args.args[2][0] == (
        'proposal:home_prepared:context', None, None, [7])


def test_comparison_envelope_does_not_decode_its_unrelated_payload(evidence):
    service, _ledger, _active = evidence
    service.replace_prepared('model_pair:trial:mixes', {'codec': 'opaque', 'data': 'not decoded', 'context': context()}, 10)
    kind, surface, operations, _generations = service.repository.replace_prepared.call_args.args
    assert (kind, surface) == ('comparison', 'model_pair:trial:mixes')
    assert operations[0][1].versions == (('alias', 'group'),)


def test_mix_orphans_are_verified_under_admission_before_sql_deletion(evidence):
    service, ledger, active = evidence
    service.repository.mix_menu_ids.return_value = ['present', 'missing']

    def contains(kind, identity):
        assert active and kind == 'opportunity'
        return identity == 'present'

    ledger.contains.side_effect = contains
    assert service.collect_unpublished_mixes(limit=2) == 1
    service.repository.remove_unpublished_mixes.assert_called_once_with(['missing'], 2)
    assert service._orphan_after == 'missing' and not active


def test_file_failure_keeps_orphan_cursor_and_sql_unchanged(evidence):
    service, ledger, active = evidence
    service.repository.mix_menu_ids.return_value = ['unknown']
    ledger.contains.side_effect = OSError('unreadable file')
    with pytest.raises(OSError):
        service.collect_unpublished_mixes()
    service.repository.remove_unpublished_mixes.assert_not_called()
    assert service._orphan_after == '' and not active


@pytest.mark.parametrize('blocked', [False, True])
def test_output_collection_combines_sql_facts_inside_the_file_protection(evidence, blocked):
    service, _ledger, active = evidence
    paths = ['kept.json', 'orphan.json']

    @contextmanager
    def candidates():
        assert active
        yield None if blocked else paths

    collector = SimpleNamespace(candidates=candidates, owners=[('group_versions', 'definition_filename')],
                                remove_unowned=Mock(return_value=CollectionProgress(files=1)))
    service.repository.protected_output_files.return_value = {'kept.json'}
    result = service.collect_output_files(collector)
    assert not active
    if blocked:
        assert result.blocked
        service.repository.protected_output_files.assert_not_called()
        collector.remove_unowned.assert_not_called()
    else:
        assert result.files == 1
        collector.remove_unowned.assert_called_once_with({'kept.json'})


def test_offer_retirement_explicitly_requests_its_mix_sql_rows(evidence):
    service, _ledger, _active = evidence
    service.retire('offer', ['expired'])
    service.repository.retire.assert_called_once_with('offer', ['expired'], mixes=True)
