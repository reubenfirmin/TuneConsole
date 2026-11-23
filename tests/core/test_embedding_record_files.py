"""Embedding packs preserve exact bytes and validate both stored path formats."""
from dataclasses import replace
import struct
import time

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import embedding_files, fao
from yt_playlist.core.embedding_paths import record_path

pytestmark = pytest.mark.usefixtures('no_database')
VECTOR = struct.pack('=ff', -.0, 1e-30)


def test_digest_packs_are_bounded_and_reuse_identical_contents(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    heads = files.write('recording', 2, [(i+1, VECTOR, 10, i+1) for i in range(129)])
    assert len({row[2] for row in heads}) == 2 and heads[127][3] == 127 and heads[128][3] == 0
    rows = [(subject, None, dimensions, digest, position)
            for subject, dimensions, digest, position, _, _ in heads]
    assert files.read_many('recording', rows[::-1]) == [(i, VECTOR) for i in range(129, 0, -1)]
    duplicate, = files.write('recording', 2, [(514, VECTOR, 11, 130)])
    assert duplicate[2] == heads[-1][2]
    assert len(list(tmp_path.rglob('*.bin'))) == 2


def test_invalid_bytes_do_not_publish_and_corrupt_packs_fail_integrity_checks(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    with pytest.raises(ValueError, match='declared dimensions'):
        files.write('artist', 2, [(1, VECTOR[:4], 10, 1)])
    assert not list(tmp_path.rglob('*.bin'))
    head, = files.write('artist', 2, [(1, VECTOR, 10, 1)])
    subject, dimensions, digest, position, _, _ = head
    fao.write(tmp_path / embedding_files.relative('artist', digest), b'broken')
    with pytest.raises(fao.CorruptFile):
        files.read_many('artist', [(subject, None, dimensions, digest, position)])


def test_selected_numeric_packs_remain_readable(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    head, = files.write('recording', 2, [(1, VECTOR, 10, 1)])
    digest = head[2]
    raw = fao.read(tmp_path / embedding_files.relative('recording', digest)).bytes
    fao.write(tmp_path / record_path('recording', 257), raw)
    assert files.read_many('recording', [(1, None, 2, (digest, 257), 0)]) == [(1, VECTOR)]


def test_collection_honors_readers_and_abandoned_file_age(tmp_path):
    files = embedding_files.EmbeddingFiles(tmp_path)
    head, = files.write('recording', 2, [(1, VECTOR, 10, 1)])
    identity = head[2]
    path = tmp_path / embedding_files.relative('recording', identity)
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
    assert {(kind, ref, candidate) for kind, ref, candidate, info in found} == {('recording', identity, path)}
    assert files.remove(path, found[0][3])
