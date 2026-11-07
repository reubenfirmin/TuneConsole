"""Conflict resolution uses mocked persistence and explicit file evidence."""
from unittest.mock import create_autospec
from contextlib import contextmanager

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment import Enrichment
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.core.metadata_runtime import Capture
from yt_playlist.core.metadata_values import Value
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles
from yt_playlist.repos.enrichment import EnrichmentRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def enrichment():
    repo = create_autospec(EnrichmentRepo, instance=True, spec_set=True)
    choices = create_autospec(MetadataChoices, instance=True, spec_set=True)
    choices.choose.return_value = 14
    choices.capture_fields.return_value = (3, {'genre': 14, 'mb_year': 15})
    repo.conflict_inputs.return_value = {'entry_id': 7, 'recording_id': 2, 'layout': 'evidence', 'conflicts': []}
    repo.provider_inputs.return_value = {'entry_id': 7, 'recording_id': 2, 'video_id': 'video',
        'layout': 'runtime', 'attempts': False, 'sources': {}, 'genre': None, 'provider_at': None}
    return Enrichment(repo, choices, create_autospec(RuntimeMetadataFiles, instance=True, spec_set=True),
                      create_autospec(MetadataDeliveries, instance=True, spec_set=True))


@pytest.mark.parametrize(('field', 'value', 'column', 'prepared'), [
    ('year', '1995', 'mb_year', '1995'), ('genre', '', 'genre', ''),
    ('bpm', '120', 'bpm', 120.0), ('bpm', '', 'bpm', None),
    ('popularity', '4', 'popularity', 4), ('energy', 'bad', 'energy', 'bad'),
])
def test_resolution_prepares_the_metadata_choice_before_marking_conflict(enrichment, field, value, column, prepared):
    events = []
    enrichment.choices.choose.side_effect = lambda *args, **kwargs: events.append('choice') or 14
    enrichment.repository.mark_resolved.side_effect = lambda *args: events.append('conflict')
    enrichment.resolve_conflict(7, field, value, now=10)
    enrichment.choices.choose.assert_called_once_with(7, column, prepared, source='conflict_resolution', now=10)
    enrichment.repository.mark_resolved.assert_called_once_with(7, field, value, 14, 10)
    assert events == ['choice', 'conflict']


def test_invalid_field_does_not_write_a_choice_or_conflict(enrichment):
    with pytest.raises(ValueError, match='not a writable enrichment field'):
        enrichment.resolve_conflict(7, 'unknown', 'value', now=10)
    assert not enrichment.choices.mock_calls
    assert not enrichment.repository.mock_calls
    assert not enrichment.resolve_enrichment_candidate(7, 'unknown', 'value', now=10)


@pytest.mark.parametrize(('current', 'offered', 'value', 'accepted'), [
    (None, [{'provider': 'a', 'value': 120}], '120', True),
    (120.0, [], '120.0', True), (None, [], '', True),
    ('Rock', [{'provider': 'a', 'value': 'Jazz'}], 'Unseen', False),
])
def test_accepts_only_a_current_or_offered_value(enrichment, current, offered, value, accepted):
    enrichment.repository.resolution_inputs.return_value = {
        'entry_id': 7, 'current': current, 'evidence': {'candidates': offered}}
    assert enrichment.resolve_enrichment_candidate(99, 'genre', value, now=10) is accepted
    enrichment.repository.resolution_inputs.assert_called_once_with(99, 'genre', column='genre')
    if accepted:
        enrichment.choices.choose.assert_called_once_with(7, 'genre', value, source='conflict_resolution', now=10)
    else:
        enrichment.choices.choose.assert_not_called()


@pytest.mark.parametrize(('offset', 'accepted'), [(0, False), (0.01, True), (-1, False)])
def test_runtime_resolution_excludes_expired_offers(enrichment, offset, accepted):
    now = ALTERNATIVE_SECONDS + 100
    enrichment.repository.resolution_inputs.return_value = {
        'entry_id': 7, 'current': 'Rock', 'evidence': {'reference': (12, 3), 'updated_at': now}}
    enrichment.files.read.return_value = {'candidates': [{'provider': 'a', 'value': 'Jazz', 'at': 100 + offset}]}
    assert enrichment.resolve_enrichment_candidate(7, 'genre', 'Jazz', now=now) is accepted
    enrichment.files.read.assert_called_once_with('conflicts', (12,), 3)
    assert enrichment.choices.choose.called is accepted


def test_missing_conflict_is_not_a_successful_resolution(enrichment):
    enrichment.repository.resolution_inputs.return_value = None
    assert not enrichment.resolve_enrichment_candidate(7, 'genre', 'Rock', now=10)
    enrichment.choices.choose.assert_not_called()


def test_choice_failure_leaves_conflict_unresolved(enrichment):
    enrichment.choices.choose.side_effect = OSError('choice unavailable')
    with pytest.raises(OSError, match='choice unavailable'):
        enrichment.resolve_conflict(7, 'genre', 'Rock', now=10)
    enrichment.repository.mark_resolved.assert_not_called()


def test_resolution_receipt_failure_propagates_after_the_choice(enrichment):
    enrichment.repository.mark_resolved.side_effect = RuntimeError('resolution interrupted')
    with pytest.raises(RuntimeError, match='resolution interrupted'):
        enrichment.resolve_conflict(7, 'genre', 'Rock', now=10)
    enrichment.choices.choose.assert_called_once_with(7, 'genre', 'Rock', source='conflict_resolution', now=10)


def conflict(status, candidates, *, field='genre'):
    return {'id': 12, 'entry_id': None, 'recording_id': 2, 'field': field,
            'status': status, 'evidence': {'candidates': candidates}}


@pytest.mark.parametrize(('status', 'offered', 'expected'), [
    (1, 'Rock', 1), (1, 'Jazz', 0), (2, 'Rock', 0), (0, 'Rock', 0),
])
def test_only_new_values_reopen_a_user_resolution(enrichment, status, offered, expected):
    row = conflict(status, [{'provider': 'a', 'value': 'Rock', 'observation_id': 9}])
    enrichment.repository.conflict_inputs.return_value['conflicts'] = [row]
    enrichment.upsert_conflict(7, 'genre', [{'provider': 'a', 'value': offered, 'observation_id': 14}], now=10)
    args = enrichment.repository.store_conflict.call_args.args
    assert args[3] == expected
    assert args[2][0]['value'] == offered
    enrichment.choices.capture_fields.assert_not_called()


@pytest.mark.parametrize(('refreshed', 'providers'), [({'a'}, ['a', 'b']), ({'a', 'b'}, ['a']), (None, ['a'])])
def test_a_failed_or_skipped_provider_keeps_its_previous_offer(enrichment, refreshed, providers):
    enrichment.repository.conflict_inputs.return_value['conflicts'] = [conflict(0, [
        {'provider': 'a', 'value': 'Rock', 'observation_id': 9},
        {'provider': 'b', 'value': 'Pop', 'observation_id': 10}])]
    enrichment.upsert_conflict(7, 'genre', [{'provider': 'a', 'value': 'Jazz', 'observation_id': 14}],
                               now=10, refreshed_providers=refreshed)
    candidates = enrichment.repository.store_conflict.call_args.args[2]
    assert [candidate['provider'] for candidate in candidates] == providers


def test_evidence_capture_finishes_before_the_conflict_repository_unit(enrichment):
    writing = False
    events = []

    @contextmanager
    def transaction():
        nonlocal writing
        writing = True
        events.append('begin conflict')
        try:
            yield
        finally:
            writing = False
            events.append('commit conflict')

    def capture(*args, **kwargs):
        assert not writing
        events.append('capture')
        return 3, {'mb_year': 15}

    def store(*args, **kwargs):
        assert writing
        events.append('store conflict')

    enrichment.repository.conflict_transaction.side_effect = transaction
    enrichment.choices.capture_fields.side_effect = capture
    enrichment.repository.store_conflict.side_effect = store
    enrichment.upsert_conflict(99, 'year', [{'provider': 'a', 'value': '1995'}], now=10)
    assert events == ['capture', 'begin conflict', 'store conflict', 'commit conflict']
    enrichment.choices.capture_fields.assert_called_once_with(7, 'a', {'mb_year': '1995'},
        purpose='conflict-offer:mb_year', body={'format': 'metadata-conflict-offer-1', 'field': 'mb_year', 'value': '1995'}, now=10)
    assert enrichment.repository.store_conflict.call_args.kwargs['scope'] == 'release'


def test_failed_offer_capture_cannot_publish_a_conflict(enrichment):
    enrichment.choices.capture_fields.side_effect = OSError('capture failed')
    with pytest.raises(OSError, match='capture failed'):
        enrichment.upsert_conflict(7, 'genre', [{'provider': 'a', 'value': 'Rock'}], now=10)
    enrichment.repository.conflict_transaction.assert_not_called()
    enrichment.repository.store_conflict.assert_not_called()


def test_runtime_reuse_does_not_extend_an_expired_offer(enrichment):
    now = ALTERNATIVE_SECONDS + 100
    facts = enrichment.repository.conflict_inputs.return_value
    facts['layout'] = 'runtime'
    enrichment.repository.alternative_references.return_value = {'a': {'provider_id': 3, 'at': now - 1}}
    enrichment.repository.reserve_conflict_identity.return_value = 12
    enrichment.files.read.return_value = {'at': 100}
    enrichment.upsert_conflict(7, 'genre', [{'provider': 'a', 'value': 'Rock'}], now=now)
    enrichment.files.publish.assert_called_once_with('conflicts', (12,), 1, {'candidates': []}, current_revision=None)
    enrichment.choices.capture_fields.assert_not_called()


def test_failed_runtime_document_does_not_advance_the_sql_head(enrichment):
    facts = enrichment.repository.conflict_inputs.return_value
    facts['layout'] = 'runtime'
    enrichment.repository.alternative_references.return_value = {}
    enrichment.repository.reserve_conflict_identity.return_value = 12
    enrichment.files.publish.side_effect = OSError('publication failed')
    with pytest.raises(OSError, match='publication failed'):
        enrichment.upsert_conflict(7, 'genre', [{'provider': 'a', 'value': 'Rock'}], now=10)
    enrichment.repository.store_conflict.assert_not_called()


@pytest.mark.parametrize(('evidence', 'status', 'identities'), [
    ({'a': 'Rock', 'b': 'Rock'}, 0, [12]), ({'a': 'Rock'}, 0, []),
    ({'a': 'Rock', 'c': 'Rock'}, 0, []), ({'a': 'Rock', 'b': 'Rock'}, 1, []),
])
def test_convergence_needs_every_previous_provider_and_two_current_answers(enrichment, evidence, status, identities):
    enrichment.repository.conflict_inputs.return_value['conflicts'] = [conflict(status, [
        {'provider': 'a', 'value': 'Rock'}, {'provider': 'b', 'value': 'Pop'}], field='mb_year')]
    enrichment.retire_agreed_conflicts(7, {'year': evidence}, now=10)
    enrichment.repository.mark_converged.assert_called_once_with('evidence', identities, 10)


@pytest.mark.parametrize(('field', 'value', 'previous', 'genre', 'wins'), [
    ('bpm', 120, None, None, True), ('bpm', 120, 'catalog_import', None, True),
    ('bpm', 120, 'first', None, False), ('bpm', 120, 'second', None, True),
    ('genre', 'Progressive Rock', 'first', 'Rock', True),
    ('genre', 'Rock', 'first', 'Progressive Rock', False),
    ('genre', 'Jazz', 'first', 'Rock', False),
    ('genre', 'Jazz', 'second', 'Rock', True),
])
def test_provider_precedence_allows_only_documented_genre_refinements(enrichment, field, value, previous, genre, wins):
    enrichment.repository.provider_inputs.return_value.update(sources={field: previous}, genre=genre)
    assert enrichment.apply_provider_fields(7, 'second', {field: value}, providers=('first', 'second'), now=10) == {}
    assert enrichment.choices.select.called is wins
    if wins:
        enrichment.choices.select.assert_called_once_with(7, field, value, 'second', now=10, observation_id=None)


def test_missing_provider_values_and_non_metadata_fields_do_not_erase_selections(enrichment):
    enrichment.apply_provider_fields(7, 'a', {'genre': '', 'bpm': None, 'art': 'cover', 'unsupported': 'x'}, now=10)
    enrichment.choices.select.assert_not_called()
    enrichment.choices.capture_fields.assert_not_called()


def test_provider_evidence_commits_before_any_selection_and_hints_retire_afterwards(enrichment):
    enrichment.repository.provider_inputs.return_value['layout'] = 'evidence'
    events = []
    enrichment.choices.capture_fields.side_effect = lambda *args, **kwargs: events.append('capture') or (3, {'mb_year': 15})
    enrichment.choices.select.side_effect = lambda *args, **kwargs: events.append('select')
    enrichment.repository.retire_entry_hints.side_effect = lambda *args: events.append('retire hints')
    assert enrichment.apply_provider_fields(99, 'a', {'year': '1995'}, now=10) == {'mb_year': 15}
    assert events == ['capture', 'select', 'retire hints']
    enrichment.choices.select.assert_called_once_with(7, 'mb_year', '1995', 'a', now=10, observation_id=15)
    enrichment.repository.conflict_transaction.assert_not_called()


def test_failed_evidence_capture_does_not_select_provider_fields(enrichment):
    enrichment.repository.provider_inputs.return_value['layout'] = 'evidence'
    enrichment.choices.capture_fields.side_effect = OSError('capture interrupted')
    with pytest.raises(OSError, match='capture interrupted'):
        enrichment.apply_provider_fields(7, 'a', {'genre': 'Jazz'}, now=10)
    enrichment.choices.select.assert_not_called()
    enrichment.repository.retire_entry_hints.assert_not_called()


def test_legacy_evidence_is_saved_even_when_a_higher_priority_provider_wins(enrichment):
    enrichment.repository.provider_inputs.return_value.update(layout='legacy', sources={'genre': 'first'})
    enrichment.apply_provider_fields(7, 'second', {'genre': 'Jazz'}, providers=('first', 'second'), now=10)
    enrichment.repository.store_finding.assert_called_once_with(7, 2, 'genre', 'second', 'Jazz', 10)
    enrichment.choices.select.assert_not_called()


@pytest.mark.parametrize('at', [10, 11])
def test_runtime_replayed_capture_cannot_overwrite_newer_state(enrichment, at):
    enrichment.repository.provider_inputs.return_value['provider_at'] = at
    captured = Capture(7, 2, 'video', 'a', b'{"genre":"Jazz"}', 'run', 10)
    assert enrichment.apply_provider_fields(7, 'a', {'genre': 'Jazz'}, delivery_id=captured, now=20) == {}
    enrichment.choices.select.assert_not_called()


@pytest.mark.parametrize('captured', [
    3, Capture(8, 2, 'video', 'a', b'{"genre":"Jazz"}', 'run', 10),
    Capture(7, 3, 'video', 'a', b'{"genre":"Jazz"}', 'run', 10),
    Capture(7, 2, 'other', 'a', b'{"genre":"Jazz"}', 'run', 10),
    Capture(7, 2, 'video', 'b', b'{"genre":"Jazz"}', 'run', 10),
    Capture(7, 2, 'video', 'a', b'{"genre":"Rock"}', 'run', 10),
])
def test_runtime_selection_rejects_a_different_captured_input_or_value(enrichment, captured):
    with pytest.raises(ValueError):
        enrichment.apply_provider_fields(7, 'a', {'genre': 'Jazz'}, delivery_id=captured, now=20)
    enrichment.choices.select.assert_not_called()


def test_provider_selection_follows_the_exact_delivery_and_observations(enrichment):
    enrichment.repository.provider_inputs.return_value['layout'] = 'evidence'
    enrichment.repository.delivery_evidence.return_value = {'provider': 'a', 'input_entry_id': 7,
        'input_recording_id': 2, 'input_video_id': 'video', 'key_contract': 'provider', 'observations': {'genre': 14}}
    enrichment.deliveries.response.return_value = {'format': 'enrichment-result-1', 'fields': {'genre': 'Jazz'}}
    assert enrichment.apply_provider_fields(7, 'a', {'genre': 'Jazz'}, delivery_id=3, now=10) == {'genre': 14}
    enrichment.choices.capture_fields.assert_not_called()
    enrichment.choices.select.assert_called_once_with(7, 'genre', 'Jazz', 'a', now=10, observation_id=14)
    enrichment.deliveries.response.return_value['fields']['genre'] = 'Rock'
    enrichment.choices.select.reset_mock()
    with pytest.raises(ValueError, match='differ'):
        enrichment.apply_provider_fields(7, 'a', {'genre': 'Jazz'}, delivery_id=3, now=10)
    enrichment.choices.select.assert_not_called()


@pytest.mark.parametrize(('entry', 'video', 'age', 'selected'), [
    (7, 'video', 0, ['genre', 'mb_year']), (8, 'other', 0, ['genre']),
    (7, 'video', ALTERNATIVE_SECONDS, []),
])
def test_cached_runtime_fields_preserve_recording_and_item_scope_and_expiry(enrichment, entry, video, age, selected):
    now = ALTERNATIVE_SECONDS + 100
    enrichment.repository.alternative_references.return_value = {'a': {'provider_id': 3, 'at': now - age}}
    enrichment.files.read.return_value = {'recording_id': 2, 'entry_id': entry, 'input_video_id': video,
                                         'fields': {'genre': 'Jazz', 'year': '1995'}}
    assert enrichment.reuse_provider_fields(7, 'a', {'genre': 'Jazz', 'year': '1995'}, now=now) is None
    assert [call.args[1] for call in enrichment.choices.select.call_args_list] == selected


@pytest.mark.parametrize(('field', 'scope', 'entry', 'selected'), [
    ('genre', 'recording', 8, True), ('mb_year', 'release', 8, False),
    ('title', 'item', 8, False), ('mb_year', 'release', 7, True),
])
def test_cached_observations_require_proven_scope(enrichment, field, scope, entry, selected):
    enrichment.repository.provider_inputs.return_value.update(layout='evidence', attempts=True)
    enrichment.repository.reuse_inputs.return_value = {field: {'id': 14, 'value': Value('value', text='answer'),
        'recording_id': 2, 'field_scope': scope, 'entry_id': entry}}
    result = enrichment.reuse_provider_fields(7, 'a', {field: 'answer'}, now=10)
    assert result == ({field: 14} if selected else {})
    assert enrichment.choices.select.called is selected
    enrichment.choices.capture_fields.assert_not_called()


def test_legacy_recording_reuse_transfers_evidence_before_selection(enrichment):
    enrichment.repository.provider_inputs.return_value.update(layout='evidence', attempts=True)
    enrichment.repository.reuse_inputs.return_value = {'genre': {'id': 14, 'value': Value('value', text='Jazz'),
        'recording_id': 2, 'field_scope': 'legacy_recording', 'entry_id': None}}
    events = []
    enrichment.choices.reuse_recording_observation.side_effect = lambda *args, **kwargs: events.append('transfer') or 15
    enrichment.choices.select.side_effect = lambda *args, **kwargs: events.append('select')
    assert enrichment.reuse_provider_fields(7, 'a', {'genre': 'Jazz'}, now=10) == {'genre': 15}
    assert events == ['transfer', 'select']


def test_cached_answer_must_match_its_retained_observation(enrichment):
    enrichment.repository.provider_inputs.return_value.update(layout='evidence', attempts=True)
    enrichment.repository.reuse_inputs.return_value = {'genre': {'id': 14, 'value': Value('value', text='Rock')}}
    with pytest.raises(ValueError, match='differ'):
        enrichment.reuse_provider_fields(7, 'a', {'genre': 'Jazz'}, now=10)
    enrichment.choices.select.assert_not_called()


@pytest.mark.parametrize(('layout', 'source', 'current', 'hint', 'selected'), [
    ('runtime', 'catalog_import', 'Rock', None, True),
    ('runtime', 'discovery_import', 'Rock', None, True),
    ('runtime', 'a', 'Rock', None, False),
    ('evidence', 'a', 'Rock', 'Rock', True),
    ('evidence', 'a', 'Jazz', 'Rock', False),
    ('legacy', 'a', 'Jazz', 'Rock', False),
])
def test_direct_findings_replace_only_current_import_hints(enrichment, layout, source, current, hint, selected):
    hints = [{'entry_id': 8, 'source': 'import', 'value': Value('value', text=hint)}] if hint else []
    enrichment.repository.hint_inputs.return_value = {'entry_id': 7, 'layout': layout,
        'fields': {'genre': {'source': source, 'current': current, 'hints': hints}}}
    enrichment.replace_catalog_hints(99, {'genre': 'Blues'})
    assert enrichment.choices.select.called is selected
    assert enrichment.repository.retire_hints.called is selected


def test_failed_hint_selection_leaves_the_hint_active(enrichment):
    enrichment.repository.hint_inputs.return_value = {'entry_id': 7, 'layout': 'runtime',
        'fields': {'genre': {'source': 'catalog_import', 'current': 'Rock', 'hints': []}}}
    enrichment.choices.select.side_effect = OSError('interrupted')
    with pytest.raises(OSError, match='interrupted'):
        enrichment.replace_catalog_hints(7, {'genre': 'Blues'})
    enrichment.repository.retire_hints.assert_not_called()
