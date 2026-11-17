"""Collection FAO identity and integrity, without a repository or database."""
from pathlib import Path

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core.collection_files import RecoveryFiles, record_id, record_path

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('identity', [0, -1, True, '1', '../1', 1.0])
def test_collection_file_requires_a_positive_database_integer(tmp_path, identity):
    with pytest.raises(ValueError, match='document ID'):
        record_path(tmp_path, identity)
    assert list(tmp_path.iterdir()) == []


def test_same_content_has_distinct_record_paths_and_independent_integrity(tmp_path):
    files = RecoveryFiles(tmp_path / 'files')
    value = dict(marker='same bytes')
    integrity = files.describe('radio-session', 'episode', value)
    assert not Path(files.root).exists(), 'Description must not publish bytes'
    first = files.publish_reserved(1, 'radio-session', 'episode', value, expected=integrity)
    second = files.publish_reserved(2, 'radio-session', 'episode', value, expected=integrity)
    assert first[0] != second[0]
    assert first[1:] == second[1:] == integrity
    assert record_id(first[0]) == 1 and record_id(second[0]) == 2
    assert files.read(first, kind='radio-session', owner='episode') == value
    assert files.read(second, kind='radio-session', owner='episode') == value
    with pytest.raises(ValueError, match='another workflow'):
        files.read(first, kind='radio-session', owner='other episode')


def test_changed_body_is_rejected_before_file_publication(tmp_path):
    files = RecoveryFiles(tmp_path / 'files')
    integrity = files.describe('library-sync', 1, dict(marker='reserved'))
    with pytest.raises(ValueError, match='changed after reservation'):
        files.publish_reserved(1, 'library-sync', 1, dict(marker='changed'), expected=integrity)
    assert not Path(files.root).exists()
