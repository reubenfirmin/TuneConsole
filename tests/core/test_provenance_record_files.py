"""Immutable calculation packs, corruption and bounded expiry without SQLite."""
import hashlib
import json
import time

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, file_collection, vibe_provenance as files
from yt_playlist.core.evidence_ledger import canonical

pytestmark = pytest.mark.usefixtures('no_database')


def write(port, values, first=257):
    records, roots = port.describe(values)
    records = [{**record, 'file_id': first + offset} for offset, record in enumerate(records)]
    staged, markers = port.stage_reserved(records)
    try:
        for publication in staged:
            publication.publish()
    finally:
        for publication in staged:
            publication.close()
    return records, roots, markers


def test_native_paths_use_reserved_ids_and_repeated_calculations_never_overwrite(tmp_path):
    port = files.ProvenanceFiles(tmp_path)
    first, roots, markers = write(port, [(1, [8, 7, 8], [3])])
    old = fao.read(tmp_path / files.file_relative(257)).bytes
    second, _, _ = write(port, [(1, [7], [3])], first=513)
    assert files.file_relative(257).as_posix() == 'by-id/01/257.json'
    assert roots == [7, 8] and markers == [(1, files.fingerprint([7, 8]), len(canonical({'inputs': [7, 8], 'artists': [3]})), 257)]
    assert fao.read(tmp_path / files.file_relative(257)).bytes == old
    assert port.read(1, 257, first[0]['digest'], first[0]['size']) == {'1': {'inputs': [7, 8], 'artists': [3]}}
    assert port.read(1, 513, second[0]['digest'], second[0]['size']) == {'1': {'inputs': [7], 'artists': [3]}}
    altered = old.replace(b'"artists":[3]', b'"artists":[4]')
    assert len(altered) == len(old) and altered != old
    fao.write(tmp_path / files.file_relative(257), altered)
    with pytest.raises(fao.CorruptFile, match='reserved bytes'):
        port.read(1, 257, first[0]['digest'], first[0]['size'])


def test_pack_bounds_and_invalid_or_changed_input_fail_before_preparing_files(tmp_path, monkeypatch):
    port = files.ProvenanceFiles(tmp_path)
    monkeypatch.setattr(fao, 'prepare', lambda *args, **kw: pytest.fail('Unexpected file write'))
    records, _roots = port.describe((pid, [1], []) for pid in range(1, 130))
    assert len(records) == 2
    assert [len(json.loads(record['raw'])['calculations']) for record in records] == [127, 2]
    for values in ([(0, [1], [])], [(1, [1], []), (1, [2], [])], [(1, [True], [])]):
        with pytest.raises(ValueError):
            port.describe(values)
    with pytest.raises(ValueError, match='changed after reservation'):
        port.stage_reserved([{**records[0], 'file_id': 1, 'size': 2}])
    monkeypatch.setattr(files, 'MAX_BYTES', 3)
    with pytest.raises(ValueError, match='byte budget'):
        port.describe([(1, [1], [])])


def test_invalid_legacy_chunk_never_replaces_required_evidence(tmp_path):
    path = tmp_path / files.relative(1)
    raw = canonical({'format': 1, 'calculations': {'1': {}, '129': {}}})
    fao.write(path, raw)
    with pytest.raises(fao.CorruptFile, match='chunk owner'):
        files.ProvenanceFiles.prune(path, fao.read(path), file_id=None, ready={'1'}, owned={'1'}, before=10**10)
    assert fao.read(path).bytes == raw


def test_collection_has_a_work_budget_keeps_readers_and_honors_pending_grace(tmp_path, monkeypatch):
    port = files.ProvenanceFiles(tmp_path)
    write(port, [(1, [7], [3])])
    legacy = tmp_path / files.relative(129)
    fao.write(legacy, canonical({'format': 1, 'calculations': {'129': {'inputs': [7], 'artists': [3]}}}))
    walked, walk = [], file_collection.walk_entries
    def counted(*args, **kwargs):
        for path in walk(*args, **kwargs):
            walked.append(path)
            yield path
    monkeypatch.setattr(file_collection, 'walk_entries', counted)
    with file_collection.retained_reader(tmp_path), port.garbage(limit=8) as candidates:
        assert list(candidates) == []
    assert walked == []
    found = {}
    for _ in range(12):
        walked.clear()
        with port.garbage(limit=8) as candidates:
            for path, content, identity, group in candidates:
                found[path] = identity, group
                port.prune(path, content, file_id=identity, ready=False if identity else set(), owned=set(),
                           before=time.time() - 30 * 86400)
        assert len(walked) <= 8
        if len(found) == 2:
            break
    assert found == {tmp_path / files.file_relative(257): (257, None), legacy: (None, 1)}
    assert all(path.exists() for path in found)
    for path, (identity, _group) in found.items():
        port.prune(path, fao.read(path), file_id=identity, ready=True if identity else {'129'}, owned={'129'}, before=10**10)
        assert path.exists()
        port.prune(path, fao.read(path), file_id=identity, ready=False if identity else set(), owned={'129'}, before=10**10)
        assert not path.exists()
