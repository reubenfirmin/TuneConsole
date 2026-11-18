"""Paid-description file publication uses reserved IDs without SQL access."""
import json

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import album_description_files as files, fao

pytestmark = pytest.mark.usefixtures('no_database')


def test_identical_paid_copy_keeps_distinct_file_owners_and_exact_original_text(tmp_path):
    raw = '{ "description": "Paid copy. é", "opaque": [1, true] }'
    first = files.publish_reserved(tmp_path, 257, raw)
    second = files.publish_reserved(tmp_path, 258, raw)
    assert first[0] == str(tmp_path / 'by-id/01/257.json')
    assert second[0] != first[0] and second[1:] == first[1:]
    assert files.describe(raw) == first[1:]
    assert files.read(*first[:2]) == raw
    assert files.read(*second[:2]) == raw
    fao.write(first[0], files.document(json.dumps({'description': 'Changed'})))
    with pytest.raises(ValueError, match='digest'):
        files.read(*first[:2])


def test_invalid_paid_copy_does_not_create_a_file(tmp_path):
    for raw in ('[]', '{"value": NaN}', json.dumps({'value': 'x' * files.MAX_BYTES})):
        with pytest.raises(ValueError):
            files.publish_reserved(tmp_path, 1, raw)
    assert not list(tmp_path.rglob('*.json'))


def test_installed_hash_paths_remain_readable_beside_reserved_files(tmp_path):
    raw = '{"status":"unknown"}'
    reference = files.publish(tmp_path, raw)
    assert files.read(*reference[:2]) == raw
    assert files.read(*files.publish_reserved(tmp_path, 1, raw)[:2]) == raw
