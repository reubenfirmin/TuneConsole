"""Recommendation ID paths, byte integrity and FAO staging without SQLite."""
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, recommendation_content as content, recommendation_outputs
from yt_playlist.core.recommendation_files import RecommendationFiles

pytestmark = pytest.mark.usefixtures('no_database')


def test_identical_bytes_have_distinct_record_owned_paths_and_checked_content(tmp_path):
    files = RecommendationFiles(tmp_path)
    encoded = content.encode('home_prepared', '0', 'Ready')
    integrity = files.describe(encoded)
    first = files.publish_reserved(41, encoded, expected=integrity)
    second = files.publish_reserved(42, encoded, expected=integrity)
    assert first[1:] == second[1:] == integrity
    assert Path(first[0]) == tmp_path / 'by-id' / '29' / '41.json'
    assert content.record_id(first[0]) == 41
    assert first[0] != second[0]
    assert Path(first[0]).stem != hashlib.sha256(encoded).hexdigest()
    reference = dict(zip(('filename', 'content_digest', 'byte_size'), first))
    assert files.read(reference, surface='home_prepared', chunk_key='0') == 'Ready'
    with pytest.raises(ValueError, match='owner'):
        files.read(reference, surface='home_tuned', chunk_key='0')
    fao.write(first[0], b'changed')
    with pytest.raises(ValueError, match='digest'):
        files.read(reference, surface='home_prepared', chunk_key='0')


@pytest.mark.parametrize('identity', [0, -1, True, '41', '../41'])
def test_invalid_ids_never_create_files(tmp_path, identity):
    files = RecommendationFiles(tmp_path)
    encoded = content.encode('home_prepared', '0', 'Ready')
    with pytest.raises(ValueError, match='ID'):
        files.publish_reserved(identity, encoded, expected=files.describe(encoded))
    assert not list(tmp_path.rglob('*'))


def test_changed_prepared_content_is_rejected_before_file_creation(tmp_path):
    files = RecommendationFiles(tmp_path)
    before = content.encode('home_prepared', '0', 'Before')
    after = content.encode('home_prepared', '0', 'After')
    with pytest.raises(ValueError, match='changed after reservation'):
        files.publish_reserved(41, after, expected=files.describe(before))
    assert not list(tmp_path.rglob('*'))


def test_bounded_cleanup_keeps_live_and_fresh_files_and_expires_abandoned_record_files(tmp_path):
    files = RecommendationFiles(tmp_path)
    encoded = content.encode('home_prepared', '0', 'Ready')
    integrity = files.describe(encoded)
    current, fresh, expired = [files.publish_reserved(identity, encoded, expected=integrity)[0]
                               for identity in (41, 42, 43)]
    age = fao.read(expired).info
    fao.write(expired, fao.Content(encoded, metadata=replace(age, ctime_ns=age.ctime_ns - 31 * 86400 * 10**9)))
    legacy = tmp_path / integrity[0][:2] / (integrity[0] + '.json')
    fao.write(legacy, encoded)
    collector = recommendation_outputs.OrphanCollector(tmp_path)
    try:
        with files.retained(), collector.candidates(limit=2) as names:
            assert names is None
        removed = 0
        while True:
            with collector.candidates(limit=2) as names:
                assert names is not None
                progress = collector.remove_unowned({current})
            assert progress.checked <= 2
            removed += progress.files
            if not progress.pending:
                break
        assert removed == 2
        assert Path(current).is_file() and Path(fresh).is_file()
        assert not Path(expired).exists() and not legacy.exists()
    finally:
        collector.close()
