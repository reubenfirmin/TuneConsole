"""Metadata choices coordinate mocked storage and files without opening SQLite."""
from contextlib import contextmanager
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles
from yt_playlist.repos.metadata_choices import MetadataChoiceRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def metadata():
    repo = create_autospec(MetadataChoiceRepo, instance=True, spec_set=True)

    @contextmanager
    def transaction():
        yield

    repo.transaction.side_effect = transaction
    repo.input.return_value = {'track': {'id': 7, 'recording_id': 8, 'video_id': 'video',
                                       'title': 'Song', 'artist': 'Artist'},
                               'layout': 'runtime', 'choice': None}
    repo.reserve_identity.return_value = 12
    repo.query_identity.return_value = (8, 'Song', 'Artist')
    repo.genre_matches.return_value = {'jazz': {}}
    return MetadataChoices(repo, create_autospec(RuntimeMetadataFiles, instance=True, spec_set=True),
                           create_autospec(MetadataDeliveries, instance=True, spec_set=True))


@pytest.mark.parametrize('layout', ['legacy', 'evidence', 'runtime'])
def test_import_fills_missing_fields_and_retains_hints_without_overwriting_selections(metadata, layout):
    metadata.repository.import_inputs.return_value = dict(layout=layout,
        track={'genre': '', 'mb_year': None, 'bpm': 120}, hints={'bpm'})
    metadata.select = Mock()
    metadata.deliveries.capture_state.return_value = 20
    metadata.repository.store_observations.side_effect = lambda delivery, fields: {field: 21 for field in fields}
    metadata.fill_import(7, {'genre': 'Jazz', 'mb_year': '2000', 'bpm': 121}, 'discovery_import', hint_source='release', now=10)
    metadata.select.assert_called_once_with(7, 'mb_year', '2000', 'discovery_import', now=10)
    if layout == 'runtime':
        metadata.repository.store_hint.assert_not_called()
        metadata.deliveries.capture_state.assert_not_called()
    else:
        assert [call.args for call in metadata.repository.store_hint.call_args_list] == [
            (layout, 7, 'genre', 'release', 'Jazz', 21 if layout == 'evidence' else None),
            (layout, 7, 'mb_year', 'release', '2000', 21 if layout == 'evidence' else None)]
        assert metadata.deliveries.capture_state.call_count == (2 if layout == 'evidence' else 0)


def test_import_hint_file_failure_does_not_publish_its_reference(metadata):
    metadata.repository.import_inputs.return_value = dict(layout='evidence', track={'mb_year': '1999'}, hints=set())
    metadata.deliveries.capture_state.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        metadata.fill_import(7, {'mb_year': '2000'}, 'discovery_import', hint_source='release', now=10)
    metadata.repository.store_hint.assert_not_called()
    metadata.repository.store_observations.assert_not_called()


@pytest.mark.parametrize('layout', ['runtime', 'evidence', 'legacy'])
def test_equivalent_recording_reuses_only_genuinely_missing_base_fields(metadata, layout):
    metadata.repository.recording_reuse_inputs.return_value = dict(layout=layout, target_entry=9,
        before={'genre': 'Jazz', 'mb_year': '2000', 'bpm': 120, 'energy': .8},
        after={'genre': None, 'mb_year': None, 'bpm': 125, 'energy': ''},
        selected={'mb_year', 'energy'}, sources={'genre': 'provider'})
    metadata.select = Mock()
    metadata.reuse_recording_selections(7, 12)
    if layout == 'runtime':
        metadata.select.assert_called_once_with(9, 'genre', 'Jazz', 'provider')
        metadata.repository.copy_recording_selection.assert_not_called()
    else:
        metadata.repository.copy_recording_selection.assert_called_once_with(layout, 7, 12, 'genre')
        metadata.select.assert_not_called()


def test_runtime_choice_publishes_file_before_installing_sql_head(metadata):
    events = []
    metadata.repository.reserve_identity.side_effect = lambda: events.append('identity') or 12
    metadata.files.publish.side_effect = lambda *args, **kwargs: events.append('file')
    metadata.repository.store.side_effect = lambda *args, **kwargs: events.append('head') or 12
    assert metadata.choose(7, 'title', 'Edited', now=10) == 12
    assert events == ['identity', 'file', 'head']
    metadata.files.publish.assert_called_once_with('choices', (12,), 1, {
        'entry_id': 7, 'recording_id': 8, 'input_video_id': 'video', 'field': 'title',
        'value': 'Edited', 'withdrawn': False, 'source': 'user', 'at': 10}, current_revision=None)
    metadata.deliveries.capture_state.assert_not_called()


def test_existing_choice_keeps_identity_and_advances_file_revision(metadata):
    metadata.repository.input.return_value['choice'] = {'id': 12, 'revision': 3}
    metadata.choose(7, 'bpm', None, now=10)
    metadata.repository.reserve_identity.assert_not_called()
    assert metadata.files.publish.call_args.args[1:3] == ((12,), 4)
    assert metadata.files.publish.call_args.kwargs == {'current_revision': 3}
    assert metadata.repository.store.call_args.args[3].kind == 'null'


def test_file_failure_does_not_publish_choice_head(metadata):
    metadata.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        metadata.choose(7, 'title', 'Edited', now=10)
    metadata.repository.store.assert_not_called()
    metadata.repository.resume_provider_query.assert_not_called()


@pytest.mark.parametrize(('field', 'value'), [('bpm', True), ('bpm', -1), ('available', 2),
                                             ('title', 12), ('unknown', 'text')])
def test_invalid_choice_is_rejected_before_a_writer_or_file(metadata, field, value):
    with pytest.raises(ValueError):
        metadata.choose(7, field, value, now=10)
    metadata.repository.transaction.assert_not_called()
    metadata.files.publish.assert_not_called()


@pytest.mark.parametrize('layout', ['legacy', 'evidence'])
def test_predecessor_choices_remain_database_only(metadata, layout):
    metadata.repository.input.return_value['layout'] = layout
    metadata.choose(7, 'bpm', '', now=10)
    assert metadata.repository.store.call_args.args[3].kind == 'blank'
    metadata.files.publish.assert_not_called()
    metadata.repository.reserve_identity.assert_not_called()


@pytest.mark.parametrize(('matches', 'key', 'reason'), [
    ({'jazz': 'jazz'}, 'jazz', None), ({}, None, 'unknown_genre'),
    ({'one': 'family', 'two': 'family'}, None, 'ambiguous_genre'),
])
def test_genre_choice_uses_unambiguous_retained_identity(metadata, matches, key, reason):
    metadata.repository.genre_matches.return_value = matches
    metadata.choose(7, 'genre', 'Jazz', now=10)
    prepared = metadata.repository.store.call_args.args[3]
    assert (prepared.genre_key, prepared.resolution_reason) == (key, reason)


def test_changed_title_resumes_provider_retry_without_resetting_other_providers(metadata):
    metadata.repository.query_identity.return_value = (8, 'Edited', 'Artist')
    metadata.choose(7, 'title', 'Edited', now=10)
    metadata.repository.resume_provider_query.assert_called_once_with(8, 'lastfm', status='retry')


def test_predecessor_capture_commits_before_the_selection_repository_transaction(metadata):
    metadata.repository.input.return_value['layout'] = 'evidence'
    events = []

    @contextmanager
    def transaction():
        events.append('begin selection')
        yield
        events.append('commit selection')

    metadata.repository.transaction.side_effect = transaction
    metadata.deliveries.capture_state.side_effect = lambda *args, **kwargs: events.append('capture') or 15
    metadata.repository.store_base.side_effect = lambda *args, **kwargs: events.append('select')
    metadata.select(7, 'title', 'Original', 'youtube', now=10)
    assert events == ['capture', 'begin selection', 'select', 'commit selection']
    metadata.deliveries.capture_state.assert_called_once_with(7, 'youtube', 'selected-field:title',
        {'format': 'catalog-field-capture-1', 'field': 'title', 'value': 'Original'}, now=10)
    assert metadata.repository.store_base.call_args.kwargs == {'delivery_id': 15, 'observation_id': None}


def test_failed_predecessor_capture_keeps_existing_selection(metadata):
    metadata.repository.input.return_value['layout'] = 'evidence'
    metadata.deliveries.capture_state.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        metadata.restore(7, 'title', 'Original', now=10)
    metadata.repository.transaction.assert_not_called()
    metadata.repository.store_base.assert_not_called()
    metadata.repository.store.assert_not_called()


def test_edit_captures_restore_evidence_before_opening_the_metadata_unit(metadata):
    metadata.repository.input.return_value['layout'] = 'evidence'
    depth = 0

    @contextmanager
    def transaction():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    def capture(*args, **kwargs):
        assert depth == 0
        return 15

    def store(*args, **kwargs):
        assert depth > 0

    metadata.repository.transaction.side_effect = transaction
    metadata.deliveries.capture_state.side_effect = capture
    metadata.repository.store.side_effect = store
    metadata.repository.store_base.side_effect = store
    metadata.edit(7, [('title', 'restore', 'Original'), ('mb_year', 'choose', '2000')])
    assert metadata.repository.store.call_count == 2
    assert metadata.repository.store_base.call_args.kwargs == {'delivery_id': 15}
    assert depth == 0


def test_restored_base_title_resumes_provider_query_even_without_a_choice(metadata):
    metadata.repository.input.side_effect = [
        dict(metadata.repository.input.return_value),
        dict(metadata.repository.input.return_value,
             track={'id': 7, 'recording_id': 8, 'video_id': 'video', 'title': 'Original', 'artist': 'Artist'}),
    ]
    metadata.repository.query_identity.return_value = (8, 'Original', 'Artist')
    metadata.edit(7, [('title', 'restore', 'Original')])
    metadata.repository.resume_provider_query.assert_called_once_with(8, 'lastfm', status='retry')


def test_supplied_observation_does_not_capture_another_delivery(metadata):
    metadata.repository.input.return_value['layout'] = 'evidence'
    metadata.select(7, 'title', 'Original', 'youtube', observation_id=20, now=10)
    metadata.deliveries.capture_state.assert_not_called()
    assert metadata.repository.store_base.call_args.kwargs == {'delivery_id': None, 'observation_id': 20}


def test_capture_fields_validates_before_publishing_and_stores_typed_observations_afterwards(metadata):
    events = []
    metadata.deliveries.capture_state.side_effect = lambda *args, **kwargs: events.append('capture') or 15
    metadata.repository.store_observations.side_effect = lambda *args: events.append('observations') or {'bpm': 20}
    body = {'format': 'catalog-provider-fields-1', 'fields': {'bpm': 120}}
    assert metadata.capture_fields(7, 'a', {'bpm': 120}, purpose='provider-fields', body=body, now=10) == (15, {'bpm': 20})
    assert events == ['capture', 'observations']
    prepared = metadata.repository.store_observations.call_args.args[1]['bpm']
    assert (prepared.number, prepared.unit) == (120, 'bpm')
    metadata.repository.transaction.assert_not_called()


def test_invalid_captured_field_cannot_publish_a_delivery(metadata):
    with pytest.raises(ValueError):
        metadata.capture_fields(7, 'a', {'bpm': True}, purpose='provider-fields', body={}, now=10)
    metadata.deliveries.capture_state.assert_not_called()
    metadata.repository.store_observations.assert_not_called()


def test_failed_observation_write_does_not_repeat_the_delivery_capture_implicitly(metadata):
    metadata.deliveries.capture_state.return_value = 15
    metadata.repository.store_observations.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError, match='interrupted'):
        metadata.capture_fields(7, 'a', {'bpm': 120}, purpose='provider-fields', body={}, now=10)
    metadata.deliveries.capture_state.assert_called_once()


def test_provider_result_retains_delivery_before_typed_observations(metadata):
    events = []
    metadata.deliveries.record.side_effect = lambda delivery: events.append('delivery') or 15
    metadata.repository.store_observations.side_effect = lambda *args, **kw: events.append('observations') or {'mb_year': 20}
    assert metadata.capture_result(7, 'provider', {'year': '2000'}, run_id='run', now=10) == (15, {'mb_year': 20})
    assert events == ['delivery', 'observations']
    delivery, = metadata.deliveries.record.call_args.args
    assert (delivery.delivery_key, delivery.input_entry_id, delivery.input_recording_id) == ('enrichment-result:run', 7, 8)
    assert delivery.response == {'format': 'enrichment-result-1', 'fields': {'year': '2000'}}
    prepared = metadata.repository.store_observations.call_args.args[1]['mb_year']
    assert prepared.text == '2000'
    assert metadata.repository.store_observations.call_args.kwargs == {'source_run_id': 'run'}
    metadata.repository.transaction.assert_not_called()


@pytest.mark.parametrize('fields', [{'bpm': True}, {'year': 2000, 'mb_year': 2001}, {'unsupported': 1}])
def test_invalid_provider_result_never_publishes_evidence(metadata, fields):
    with pytest.raises(ValueError):
        metadata.capture_result(7, 'provider', fields, run_id='run', now=10)
    metadata.deliveries.record.assert_not_called()
    metadata.repository.store_observations.assert_not_called()


@pytest.fixture
def result_evidence(metadata):
    metadata.repository.delivery_evidence.return_value = {
        'provider': 'provider', 'key_contract': 'capture', 'delivery_key': 'enrichment-result:run',
        'input_entry_id': 7, 'input_recording_id': 8, 'input_video_id': 'video', 'observations': {'bpm': 20}}
    metadata.deliveries.response.return_value = {'format': 'enrichment-result-1', 'fields': {'bpm': 120, 'gain': 2}}
    return metadata


def test_provider_result_evidence_requires_exact_values_but_allows_field_subset(result_evidence):
    metadata = result_evidence
    assert metadata.result_evidence(7, 'provider', 15, {'bpm': 120}, run_id='run') == {'bpm': 20}
    with pytest.raises(ValueError, match='fields differ'):
        metadata.result_evidence(7, 'provider', 15, {'bpm': 120}, run_id='run', complete=True)
    with pytest.raises(ValueError, match='fields differ'):
        metadata.result_evidence(7, 'provider', 15, {'bpm': 121}, run_id='run')
    metadata.repository.transaction.assert_not_called()


@pytest.mark.parametrize('changed', [
    {'input_recording_id': 9}, {'input_entry_id': 9}, {'input_video_id': 'other'},
    {'provider': 'other'}, {'key_contract': 'legacy_record'}, {'delivery_key': 'enrichment-result:other'},
])
def test_provider_result_evidence_rejects_other_subjects_and_runs_before_reading_files(result_evidence, changed):
    metadata = result_evidence
    metadata.repository.delivery_evidence.return_value.update(changed)
    with pytest.raises(ValueError):
        metadata.result_evidence(7, 'provider', 15, {'bpm': 120}, run_id='run')
    metadata.deliveries.response.assert_not_called()
