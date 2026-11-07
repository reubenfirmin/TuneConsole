"""Capture identity and file reuse decisions use mocked SQL facts."""
from contextlib import nullcontext
import hashlib
import sqlite3
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
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
