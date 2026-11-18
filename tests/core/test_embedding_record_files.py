"""Native pack identity and exact binary reads with database access disabled."""
from dataclasses import replace
import struct
import time

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import embedding_files, fao, file_collection
from yt_playlist.core.embedding_paths import record_path

pytestmark = pytest.mark.usefixtures('no_database')
VECTOR = struct.pack('=ff', -.0, 1e-30)


def test_native_packs_are_bounded_and_their_paths_do_not_depend_on_contents(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    records = files.describe('recording', 2, [(i+1, VECTOR, 10, i+1) for i in range(129)])
    assert [len(record['rows']) for record in records] == [128, 1]
    for identity, record in zip((257, 258), records, strict=True):
        record['file_id'] = identity
    heads = files.write_reserved(records)
    assert (tmp_path / 'recording/by-id/01/257.bin').is_file()
    assert (tmp_path / 'recording/by-id/02/258.bin').is_file()
    rows = [(subject, None, dimensions, (digest, identity), position)
            for subject, dimensions, digest, position, order, built, identity in heads]
    assert files.read_many('recording', rows[::-1]) == [(i, VECTOR) for i in range(129, 0, -1)]
    duplicate = dict(records[1], file_id=514)
    files.write_reserved([duplicate])
    assert fao.read(tmp_path / record_path('recording', 514)).bytes == fao.read(tmp_path / record_path('recording', 258)).bytes


def test_changed_or_corrupt_native_pack_does_not_pass_integrity_checks(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    record, = files.describe('artist', 2, [(1, VECTOR, 10, 1)])
    record['file_id'] = 1
    with pytest.raises(ValueError, match='changed after reservation'):
        files.write_reserved([record | {'rows': ((1, VECTOR[::-1], 10, 1),)}])
    assert not list(tmp_path.rglob('*.bin'))
    head, = files.write_reserved([record])
    subject, dimensions, digest, position, order, built, identity = head
    fao.write(tmp_path / record_path('artist', identity), b'broken')
    with pytest.raises(fao.CorruptFile):
        files.read_many('artist', [(subject, None, dimensions, (digest, identity), position)])


def test_installed_hash_packs_remain_readable_without_conversion(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    record, = files.describe('recording', 2, [(1, VECTOR, 10, 1)])
    fao.write(tmp_path / embedding_files.relative('recording', record['digest']), record['raw'])
    assert files.read_many('recording', [(1, None, 2, record['digest'], 0)]) == [(1, VECTOR)]


def test_collection_honors_readers_and_abandoned_file_age(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    records = files.describe('recording', 2, [(1, VECTOR, 10, 1)])
    records[0]['file_id'] = 1
    files.write_reserved(records)
    path = tmp_path / record_path('recording', 1)
    with files.garbage(time.time() - 30*86400, limit=256) as candidates:
        assert list(candidates) == []
    with fao.read(path) as stream:
        raw, info = stream.read(), stream.info
    fao.write(path, fao.Content(raw, metadata=replace(info, ctime_ns=info.ctime_ns - 31*86400*10**9)))
    with files.retained(), files.garbage(time.time() - 30*86400, limit=256) as candidates:
        assert list(candidates) == []
    found = []
    for _ in range(8):
        with files.garbage(time.time() - 30*86400, limit=256) as candidates:
            found.extend(candidates)
        if found:
            break
    assert {(kind, identity, candidate) for kind, identity, candidate, info in found} == {('recording', 1, path)}
    assert files.remove(path, found[0][3])
