"""Capture identity and file reuse decisions use mocked SQL facts."""
from contextlib import nullcontext
import hashlib
import sqlite3
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
from yt_playlist.core.metadata_deliveries import Delivery
from yt_playlist.core.metadata_delivery_files import MetadataDeliveryFiles
from yt_playlist.repos.metadata_deliveries import MetadataDeliveryRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def captures():
    repo = create_autospec(MetadataDeliveryRepo, instance=True, spec_set=True)
    files = create_autospec(MetadataDeliveryFiles, instance=True, spec_set=True)
    repo.transaction.side_effect = nullcontext
    files.retained.side_effect = nullcontext
    repo.subject.return_value = {'recording_id': 8, 'video_id': 'video', 'source_ref': None}
    repo.capture_head.return_value = None
    repo.by_contract.return_value = None
    repo.response_reference.return_value = None
    repo.insert.return_value = 10
    files.encode.return_value = b'encoded'
    files.publish.return_value = ('file', hashlib.sha256(b'encoded').hexdigest(), 7, 100)
    return MetadataDeliveries(repo, files)


def test_capture_publishes_file_then_row_then_head(captures):
    events = []
    reference = captures.files.publish.return_value
    captures.files.publish.side_effect = lambda *args: events.append('file') or reference
    captures.repository.insert.side_effect = lambda *args: events.append('row') or 10
    captures.repository.set_capture_head.side_effect = lambda *args: events.append('head')
    assert captures.capture_state(7, 'youtube', 'catalog-input', {'value': 1}, now=10) == 10
    assert events == ['file', 'row', 'head']
    values, = captures.repository.insert.call_args.args
    assert values[:2] == ('youtube', 'capture')
    assert values[5:9] == (7, 8, 'video', 10)
    captures.repository.set_capture_head.assert_called_once_with(7, 'youtube', 'catalog-input', 10)


def test_unchanged_input_reuses_head_without_manufacturing_a_new_capture(captures):
    captures.repository.capture_head.return_value = {
        'id': 4, 'input_recording_id': 8, 'input_video_id': 'video',
        'raw_filename': 'file', 'raw_digest': 'digest', 'raw_byte_size': 7, 'raw_offset': 100}
    captures.files.read_encoded.return_value = b'encoded'
    assert captures.capture_state(7, 'youtube', 'catalog-input', {'value': 1}, now=20) == 4
    captures.repository.insert.assert_not_called()
    captures.files.publish.assert_not_called()
    captures.repository.set_capture_head.assert_not_called()


def test_new_capture_can_reuse_verified_bytes_without_reusing_capture_identity(captures):
    reference = captures.files.publish.return_value
    captures.repository.response_reference.return_value = reference
    captures.files.read_encoded.return_value = b'encoded'
    assert captures.capture_state(7, 'youtube', 'catalog-input', {'value': 1}, now=20) == 10
    captures.files.publish.assert_not_called()
    captures.repository.insert.assert_called_once()


def test_digest_equality_requires_matching_bytes(captures):
    captures.repository.response_reference.return_value = captures.files.publish.return_value
    captures.files.read_encoded.return_value = b'different'
    with pytest.raises(sqlite3.IntegrityError, match='collision'):
        captures.capture_state(7, 'youtube', 'catalog-input', {}, now=20)
    captures.repository.insert.assert_not_called()


def test_failed_publication_cannot_advance_sql_capture_head(captures):
    captures.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        captures.capture_state(7, 'youtube', 'catalog-input', {}, now=20)
    captures.repository.insert.assert_not_called()
    captures.repository.set_capture_head.assert_not_called()


def test_response_acquires_file_retention_before_reading_its_sql_reference(captures):
    from contextlib import contextmanager

    events = []

    @contextmanager
    def retained():
        events.append('retain')
        yield
        events.append('release')

    captures.files.retained.side_effect = retained
    reference = ('file', 'digest', 80, 100)
    captures.repository.delivery_reference.side_effect = lambda identity: events.append('reference') or reference
    captures.files.read_encoded.side_effect = lambda ref: events.append('read') or b'{"value":{"fields":{"genre":"Jazz"}}}'
    assert captures.response(10) == {'fields': {'genre': 'Jazz'}}
    assert events == ['retain', 'reference', 'read', 'release']
    captures.files.read_encoded.assert_called_once_with(reference)


def test_delivery_without_a_response_does_not_read_files(captures):
    from yt_playlist.core.metadata_deliveries import NO_RESPONSE

    captures.repository.delivery_reference.return_value = (None, None, None, None)
    assert captures.response(10) is NO_RESPONSE
    captures.files.read_encoded.assert_not_called()


def test_provider_delivery_commits_only_after_its_response_is_published(captures):
    events = []
    reference = captures.files.publish.return_value
    captures.files.publish.side_effect = lambda encoded: events.append('file') or reference
    captures.repository.insert.side_effect = lambda values: events.append('row') or 10
    delivery = Delivery('provider', 'result:run', 'item', 'video', 20, 7, 8, 'video', response={'bpm': 120})
    assert captures.record(delivery) == 10
    assert events == ['file', 'row']
    values, = captures.repository.insert.call_args.args
    assert values[:10] == ('provider', 'capture', 'result:run', 'item', 'video', 7, 8, 'video', 20, delivery.contract_digest())
    captures.repository.set_capture_head.assert_not_called()


def test_provider_delivery_rejects_reassociated_input_before_publishing(captures):
    captures.repository.subject.return_value['recording_id'] = 9
    with pytest.raises(ValueError, match='subject changed'):
        captures.record(Delivery('provider', 'result:run', 'item', 'video', 20, 7, 8, 'video', response={}))
    captures.files.publish.assert_not_called()
    captures.repository.insert.assert_not_called()


@pytest.mark.parametrize('changed', [{}, {'raw_digest': 'changed'}, {'input_recording_id': 9}])
def test_provider_delivery_replay_verifies_exact_evidence_and_keeps_first_capture(captures, changed):
    digest = hashlib.sha256(b'encoded').hexdigest()
    captures.repository.by_contract.return_value = {
        'id': 4, 'provider': 'provider', 'key_contract': 'capture', 'delivery_key': 'result:run',
        'provider_entity_type': 'item', 'provider_entity_id': 'video', 'input_entry_id': 7,
        'input_recording_id': 8, 'input_video_id': 'video', 'raw_digest': digest,
        'raw_filename': 'file', 'raw_byte_size': 7, 'raw_offset': 100, **changed}
    captures.files.read_encoded.return_value = b'encoded'
    delivery = Delivery('provider', 'result:run', 'item', 'video', 99, 7, 8, 'video', response={})
    if changed:
        with pytest.raises(sqlite3.IntegrityError, match='captured evidence'):
            captures.record(delivery)
    else:
        assert captures.record(delivery) == 4
    captures.files.publish.assert_not_called()
    captures.repository.insert.assert_not_called()


def test_provider_delivery_without_response_has_no_file_reference(captures):
    assert captures.record(Delivery('provider', 'lookup:run', 'lookup', None, 20)) == 10
    values, = captures.repository.insert.call_args.args
    assert values[10:14] == (None, None, None, None)
    captures.files.encode.assert_not_called()
    captures.files.publish.assert_not_called()


def test_invalid_provider_delivery_never_acquires_a_writer(captures):
    with pytest.raises(ValueError, match='contract'):
        captures.record(Delivery('', 'lookup:run', 'lookup', None, 20))
    captures.repository.transaction.assert_not_called()
    captures.files.encode.assert_not_called()
