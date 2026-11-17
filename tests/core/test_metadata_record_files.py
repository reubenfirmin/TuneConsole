"""Reserved metadata file reads and writes never open a database."""
import hashlib

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, metadata_file_contract as contract
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles

pytestmark = pytest.mark.usefixtures('no_database')


def test_exact_document_uses_its_reserved_id_and_checks_owner_and_receipt(tmp_path):
    files = RuntimeMetadataFiles(tmp_path)
    record = dict(contract.describe('native', ('provider:recording:é',), 3, {'values': [0.7]}), file_id=257)
    with files.write_reserved([record]):
        path = tmp_path / 'by-id/01/257.json'
        encoded = fao.read(path).bytes
        reference = 257, hashlib.sha256(encoded).hexdigest(), len(encoded)
        assert files.read('native', record['owner'], 3, reference=reference) == record['data']
    for kind, owner, revision in [('native', ('other',), 3), ('choices', (1,), 3), ('native', record['owner'], 4)]:
        with pytest.raises(ValueError, match='owner or checksum'):
            files.read(kind, owner, revision, reference=reference)
    with pytest.raises(ValueError, match='SQL receipt'):
        files.read('native', record['owner'], 3, reference=(257, '0' * 64, len(encoded)))
    fao.write(path, encoded + b' ')
    with pytest.raises(ValueError, match='SQL receipt'):
        files.read('native', record['owner'], 3, reference=reference)


def test_publication_rejects_changed_bytes_before_creating_a_file(tmp_path):
    files = RuntimeMetadataFiles(tmp_path)
    record = dict(contract.describe('choices', (9,), 1, {'value': 'Jazz'}), file_id=1)
    record['data']['value'] = 'Rock'
    with pytest.raises(ValueError, match='changed after reservation'), files.write_reserved([record]):
        pass
    assert not list(tmp_path.rglob('*.json'))


def test_installed_files_and_native_files_can_be_read_together(tmp_path):
    files = RuntimeMetadataFiles(tmp_path)
    files.publish('choices', (1,), 1, {'value': 'old'})
    record = dict(contract.describe('choices', (1,), 2, {'value': 'new'}), file_id=2)
    with files.write_reserved([record]):
        assert files.read('choices', (1,), 1) == {'value': 'old'}
        assert files.read('choices', (1,), 2, reference=(2, record['digest'], record['size'])) == {'value': 'new'}
