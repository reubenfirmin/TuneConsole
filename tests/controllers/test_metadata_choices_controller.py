"""Metadata choices coordinate mocked storage and files without opening SQLite."""
from contextlib import contextmanager
from unittest.mock import create_autospec

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
    return MetadataChoices(repo, create_autospec(RuntimeMetadataFiles, instance=True, spec_set=True),
                           create_autospec(MetadataDeliveries, instance=True, spec_set=True))


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
