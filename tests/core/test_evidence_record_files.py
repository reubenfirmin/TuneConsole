"""Listening evidence file ports use detached, exact SQL ownership receipts."""
import hashlib

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.evidence_record_files import EvidenceRecordFiles

pytestmark = pytest.mark.usefixtures('no_database')


def test_new_identity_is_distinct_from_integrity_and_pending_files_are_hidden(tmp_path):
    value = {'id': 'event', 'value': 'é'}
    encoded = canonical(value)
    references = {('response', 'event'): (257, hashlib.sha256(encoded).hexdigest(), len(encoded))}
    files = EvidenceRecordFiles(tmp_path, 1024, references=references)
    assert files.put('response', 'event', value)
    assert files._path('response', 'event') == tmp_path / 'response/01/257.json'
    assert EvidenceRecordFiles(tmp_path, 1024).records('response') == []
    assert files.get('response', 'event') == value
    fao.write(files._path('response', 'event'), canonical({'id': 'event', 'value': 'different'}))
    with pytest.raises(ValueError):
        files.get('response', 'event')


def test_reserved_bytes_cannot_change_before_file_publication(tmp_path):
    files = EvidenceRecordFiles(tmp_path, 1024, references={('artifact', 'semantic'): (1, 'a' * 64, 10)})
    with pytest.raises(ValueError, match='changed after reservation'):
        files.put('artifact', 'semantic', {'value': 'different'})
    assert not list(tmp_path.rglob('*.json'))
