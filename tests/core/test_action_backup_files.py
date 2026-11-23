"""Retained backup bytes and custody with database access disabled."""
import hashlib
from pathlib import Path

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import action_backup_files as backups, fao

pytestmark = pytest.mark.usefixtures('no_database')


def test_retained_backup_keeps_exact_bytes_and_age(tmp_path):
    source = tmp_path / 'original.json'
    encoded = b'{"ytm_playlist_id":"REMOTE","tracks":[]}'
    fao.write(source, encoded)
    age = fao.read(source).info
    files = backups.ActionBackupFiles(tmp_path / 'retained')
    reference, _ = files.reference(str(source))
    retained = files.retain(source, expected=reference[1:3])
    identity = backups.record_id(retained)
    assert len(identity) == 32
    assert Path(retained) == backups.record_path(files.root, identity)
    assert fao.read(retained).bytes == encoded
    assert fao.read(retained).info.ctime_ns == age.ctime_ns


def test_source_changed_before_publication_cannot_publish_a_retained_copy(tmp_path):
    source = tmp_path / 'original.json'
    fao.write(source, b'original')
    expected = hashlib.sha256(b'original').hexdigest(), len(b'original')
    fao.write(source, b'changed')
    files = backups.ActionBackupFiles(tmp_path / 'retained')
    with pytest.raises(ValueError, match='changed before publication'):
        files.retain(source, expected=expected)
    assert not list(files.root.rglob('*'))


@pytest.mark.parametrize('identity', [0, -1, True, '41', '../41'])
def test_invalid_backup_identity_is_rejected_before_reading_or_writing_files(tmp_path, identity):
    with pytest.raises(ValueError, match='ID'):
        backups.record_path(tmp_path / 'retained', identity)
    assert not list(tmp_path.rglob('*'))
