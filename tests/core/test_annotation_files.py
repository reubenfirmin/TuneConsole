"""Annotation bytes and leases use detached SQL facts and no database access."""
from pathlib import Path

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import audio_annotation_files as files, file_collection

pytestmark = pytest.mark.usefixtures('no_database')


def document(**values):
    return dict(format=files.FILE_FORMAT, run_key='run', kind='session', taxonomy_version='v1',
                revision=1, prediction={'captured': True}, **values)


def test_reserved_identity_supplies_the_path_while_digest_verifies_bytes(tmp_path):
    owner = files.AnnotationFiles(tmp_path)
    value = document()
    integrity = owner.describe(value)
    assert not list(tmp_path.iterdir())
    with owner.retained(), owner.write_reserved(513, value, expected=integrity) as reference:
        filename, digest, size = reference
        assert Path(filename) == tmp_path / 'by-id' / '01' / '513.json'
        assert files.record_id(filename) == 513 and digest == integrity[0] and size == integrity[1]
        assert files.read(filename, expected_digest=digest) == value
        with file_collection.collector(tmp_path) as admitted:
            assert not admitted
    with file_collection.collector(tmp_path) as admitted:
        assert admitted
    with pytest.raises(ValueError, match='content hash'):
        files.read(filename, expected_digest='0' * 64)


@pytest.mark.parametrize('identity', [0, -1, True, '1', '../1'])
def test_invalid_reserved_identity_creates_no_files(tmp_path, identity):
    owner = files.AnnotationFiles(tmp_path)
    with pytest.raises(ValueError, match='positive integer'), owner.write_reserved(
            identity, document(), expected=owner.describe(document())):
        pytest.fail('Invalid annotation identity admitted')
    assert not list(tmp_path.iterdir())


def test_changed_annotation_after_reservation_creates_no_files(tmp_path):
    owner = files.AnnotationFiles(tmp_path)
    expected = owner.describe(document())
    changed = {**document(), 'prediction': {'changed': True}}
    with pytest.raises(ValueError, match='changed after reservation'), owner.write_reserved(1, changed, expected=expected):
        pytest.fail('Changed annotation admitted')
    assert not list(tmp_path.iterdir())


def test_read_checks_captured_judgment_identity(tmp_path):
    owner = files.AnnotationFiles(tmp_path)
    with owner.write_reserved(9, document(), expected=owner.describe(document())) as (filename, digest, _):
        row = dict(run_id='run', legacy_run_key=None, kind='session', taxonomy_version='v1',
                   revision=2, model_output_filename=filename, model_output_digest=digest)
        with pytest.raises(ValueError, match='judgment identity'):
            owner.read(row)
