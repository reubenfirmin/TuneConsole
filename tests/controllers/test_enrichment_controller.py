"""Conflict resolution uses mocked persistence and explicit file evidence."""
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment import Enrichment
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles
from yt_playlist.repos.enrichment import EnrichmentRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def enrichment():
    repo = create_autospec(EnrichmentRepo, instance=True, spec_set=True)
    choices = create_autospec(MetadataChoices, instance=True, spec_set=True)
    choices.choose.return_value = 14
    return Enrichment(repo, choices, create_autospec(RuntimeMetadataFiles, instance=True, spec_set=True))


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
