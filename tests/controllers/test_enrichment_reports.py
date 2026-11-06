from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_reports import EnrichmentReports
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.repos.enrichment_reports import EnrichmentReportsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def reports():
    repository = create_autospec(EnrichmentReportsRepo, instance=True, spec_set=True)
    read_document = Mock(side_effect=AssertionError('Unexpected file read'))
    return EnrichmentReports(repository, read_document, now_fn=lambda: 1000000)


def conflict(*, field='genre', current='Jazz', candidates=(), **facts):
    return {'track_id': 1, 'title': 'Song', 'artist': 'Artist', 'video_id': 'v1',
            'mb_recording_id': None, 'field': field, 'evidence': {'candidates': list(candidates)},
            'current': current, 'selected_source': None, 'findings': [], 'overridden': False, **facts}


@pytest.mark.parametrize('current,values', [
    ('Jazz', [' jazz ', 'JAZZ']), (0.5, ['0.500', 0.5]), (None, ['', None]),
])
def test_choices_merge_equivalent_values_and_preserve_all_sources(reports, current, values):
    row = conflict(field='energy' if current == .5 else 'genre', current=current,
        candidates=[{'provider': 'one', 'value': values[0]}, {'provider': 'two', 'value': values[1]}])
    reports.repository.enrichment_conflict_detail.return_value = row
    result = reports.enrichment_conflict_detail(1, row['field'])
    assert result['can_resolve']
    assert len(result['options']) == 1
    option = result['options'][0]
    assert option['current'] and option['providers'] == ['one', 'two']
    assert option['label'] == ('Not set' if current is None else current)
    assert 'evidence' not in result and 'findings' not in result
    reports.read_document.assert_not_called()


def test_saved_choice_keeps_its_position_and_stable_id_after_resolution(reports):
    candidates = [{'provider': 'one', 'value': 'Jazz'}, {'provider': 'two', 'value': 'Blues'}]
    reports.repository.enrichment_conflict_detail.return_value = conflict(current='Jazz', candidates=candidates)
    before = reports.enrichment_conflict_detail(1, 'genre')['options']
    reports.repository.enrichment_conflict_detail.return_value = conflict(current='Blues', candidates=candidates, overridden=True)
    after = reports.enrichment_conflict_detail(1, 'genre')['options']
    assert [option['id'] for option in before] == [option['id'] for option in after]
    assert [option['value'] for option in after] == ['Jazz', 'Blues']
    assert after[1]['current'] and after[1]['source_note'] == 'Your choice'


def test_current_value_outside_candidates_retains_its_provenance(reports):
    reports.repository.enrichment_conflict_detail.return_value = conflict(current='Fusion',
        candidates=[{'provider': 'one', 'value': 'Jazz'}], selected_source='manual',
        findings=[{'provider': 'old', 'value': 'fusion'}, {'provider': 'ignored', 'value': 'Rock'}])
    options = reports.enrichment_conflict_detail(1, 'genre')['options']
    assert options[0]['value'] == 'Fusion'
    assert options[0]['providers'] == ['manual', 'old']
    assert options[0]['source_note'] == ''


@pytest.mark.parametrize('current,note', [(None, 'No saved value'), ('Mystery', 'Saved locally; original source not recorded')])
def test_unknown_field_and_missing_source_have_clear_labels(reports, current, note):
    reports.repository.enrichment_conflict_detail.return_value = conflict(field='unknown', current=current)
    result = reports.enrichment_conflict_detail(1, 'unknown')
    assert not result['can_resolve']
    assert result['options'][0]['source_note'] == note


def test_runtime_conflicts_keep_only_unexpired_candidate_evidence(reports):
    reports.repository.unresolved_conflicts_for_playlist.return_value = [
        {'track_id': 1, 'field': 'genre', 'evidence': {'reference': (7, 2), 'updated_at': 1000000}}]
    reports.read_document.side_effect = None
    reports.read_document.return_value = {'candidates': [
        {'provider': 'fresh', 'value': 'Jazz'},
        {'provider': 'expired', 'value': 'Rock', 'at': 1000000 - ALTERNATIVE_SECONDS},
    ]}
    assert reports.unresolved_conflicts_for_playlist(3) == [
        {'track_id': 1, 'field': 'genre', 'candidates': [{'provider': 'fresh', 'value': 'Jazz'}]}]
    reports.read_document.assert_called_once_with('conflicts', (7,), 2)


def test_genre_provenance_combines_provider_values_and_unrecorded_conflict_sources(reports):
    reports.repository.genre_provenance_facts.return_value = {
        'track': {'id': 1, 'video_id': 'v1', 'title': 'Song', 'artist': 'Artist', 'genre': 'Jazz'},
        'runtime': False, 'provider_references': [],
        'candidates': [{'provider': 'one', 'value': 'Jazz'}, {'provider': 'two', 'value': 'Jazz'}],
        'conflict': {'candidates': [{'provider': 'one', 'value': 'Jazz'}, {'provider': None, 'value': 'Blues'}]}}
    result = reports.genre_provenance(1)
    assert result['options'] == [{'value': 'Jazz', 'providers': ['one', 'two']},
                                 {'value': 'Blues', 'providers': ['unknown']}]
    assert len(result['candidates']) == 3
    reports.read_document.assert_not_called()


def test_runtime_provenance_skips_expired_and_missing_provider_documents(reports):
    reports.repository.genre_provenance_facts.return_value = {
        'track': {'id': 1, 'video_id': 'v1', 'title': 'Song', 'artist': 'Artist', 'genre': 'Jazz'},
        'runtime': True, 'candidates': [], 'conflict': None,
        'provider_references': [
            {'recording_id': 8, 'provider_id': 1, 'provider': 'expired', 'at': 1000000 - ALTERNATIVE_SECONDS},
            {'recording_id': 8, 'provider_id': 2, 'provider': 'fresh', 'at': 999999},
            {'recording_id': 8, 'provider_id': 3, 'provider': 'missing', 'at': 999998},
        ]}
    reports.read_document.side_effect = [{'fields': {'genre': 'Jazz'}}, None]
    assert reports.genre_provenance(1)['options'] == [{'value': 'Jazz', 'providers': ['fresh']}]
    assert [call.args for call in reports.read_document.call_args_list] == [
        ('alternatives', (8, 2), 999999.), ('alternatives', (8, 3), 999998.)]


@pytest.mark.parametrize('rows,buckets,expected', [
    ([], 3, []), ([{'first': 10, 'last': 10}], 3, [{'t': 10, 'n': 1, 'refresh': 0}]),
    ([{'first': 10, 'last': 10}, {'first': 20, 'last': 50}], 4,
     [{'t': 20., 'n': 2, 'refresh': 0}, {'t': 30., 'n': 2, 'refresh': 0},
      {'t': 40., 'n': 2, 'refresh': 0}, {'t': 50., 'n': 2, 'refresh': 1}]),
])
def test_timeline_counts_first_passes_once_and_latest_refresh_separately(reports, rows, buckets, expected):
    reports.repository.processed_timestamps.return_value = rows
    assert reports.processed_timeline(buckets) == expected
