"""Shared-model native paths preserve digest verification without SQLite."""
import hashlib

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, vibe_model_files as files

pytestmark = pytest.mark.usefixtures('no_database')


def model():
    return {'format': 2, 'vibe': 'relax', 'configuration': {}, 'inputs': [
        {'measurement': 1, 'recording': 2, 'artists': [3], 'score': .8, 'support': .6,
         'source': 'provider/v', 'version': 'v', 'input_digest': 'digest'}]}


def test_model_paths_use_reserved_ids_and_integrity_still_checks_exact_bytes(tmp_path):
    port, value = files.VibeModelFiles(tmp_path), model()
    generation, raw = port.describe(value)
    for identity in (257, 513):
        with port.stage_reserved(identity, generation, raw).prepared as prepared:
            prepared.publish()
        assert port.read(generation, identity) == value
    assert files.relative(257).as_posix() == 'by-id/01/257.json'
    assert fao.read(tmp_path / files.relative(257)).bytes == fao.read(tmp_path / files.relative(513)).bytes
    fao.write(tmp_path / files.relative(257), b'{}')
    with pytest.raises(fao.CorruptFile, match='differs'):
        port.read(generation, 257)
    with pytest.raises(ValueError, match='changed after reservation'):
        port.stage_reserved(3, generation, b'{}')


def test_installed_hash_model_remains_readable_and_collection_identifies_both_formats(tmp_path):
    port, value = files.VibeModelFiles(tmp_path), model()
    generation, raw = port.describe(value)
    fao.write(tmp_path / files.relative(generation), raw)
    staged = port.stage_reserved(1, generation, raw)
    try:
        staged.publish()
    finally:
        staged.close()
    assert port.read(generation) == value
    found = set()
    for _ in range(4):
        with port.collection() as work:
            found.update(work.generations(32))
            work.save()
    assert found == {generation, 1}
