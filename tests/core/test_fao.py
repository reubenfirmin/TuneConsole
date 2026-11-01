"""Independent file contract checks, including old plain formats and failures."""
import bz2
import hashlib
import os
from pathlib import Path
import time

import pytest

from yt_playlist.core import fao


def test_exact_bytes_range_and_physical_accounting(tmp_path):
    path = tmp_path / 'binary.png'
    data = bytes(range(256)) * 1000
    fao.write_bytes(path, data)
    assert bz2.decompress(path.read_bytes()) == data
    assert fao.read_bytes(path) == data
    info = fao.stat(path)
    assert info.logical_bytes == len(data)
    assert info.compressed_bytes == path.stat().st_size
    assert info.allocated_bytes == path.stat().st_blocks * 512
    with fao.open_reader(path) as stream:
        stream.seek(12000)
        assert stream.read(300) == data[12000:12300]
        stream.seek(10)
        assert stream.read(5) == data[10:15]


def test_conversion_restore_preserve_both_clocks_and_no_duplicate(tmp_path):
    source, target = tmp_path / 'old.json', tmp_path / 'restored.json'
    source.write_bytes(b'{"old":true}')
    old = time.time_ns() - 181 * 86400 * 10**9
    os.utime(source, ns=(old, old))
    before = fao.stat(source)
    assert not before.compressed
    assert fao.convert(source)
    assert not fao.convert(source)
    fao.copy(source, target)
    for path in (source, target):
        info = fao.stat(path)
        assert info.mtime_ns == before.mtime_ns
        assert info.ctime_ns == before.ctime_ns
        assert fao.read_bytes(path) == b'{"old":true}'
    assert set(fao.listing(tmp_path)) == {source, target}
    for days in (7, 30, 180):
        assert fao.Retention(days, 'mtime').eligible(fao.stat(target), time.time())
        assert not fao.Retention(days, 'ctime').eligible(fao.stat(target), time.time())


def test_failed_publication_and_stale_conversion_keep_last_good(tmp_path, monkeypatch):
    path = tmp_path / 'data'
    fao.write_bytes(path, b'first')
    original = path.read_bytes()
    def no_space(*args):
        raise OSError(28, 'No space left on device')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', no_space)
        with pytest.raises(OSError):
            fao.write_bytes(path, b'second')
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.fao-pending-*'))
    old = fao.stat(path)
    fao.write_bytes(path, b'new publication')
    with pytest.raises(fao.FileChanged):
        fao._write(path, [b'stale'], max_bytes=100, expected=old.identity)
    assert fao.read_bytes(path) == b'new publication'
    assert not fao.delete(path, expected=old.identity)


def test_lease_fences_collection_and_releases_temporary_copy(tmp_path):
    path = tmp_path / 'model.bin'
    fao.write_bytes(path, b'original model')
    with fao.materialize(path) as lease:
        assert lease.read_bytes() == b'original model'
        assert not fao.delete(path)
        fao.write_bytes(path, b'updated model')
        assert lease.read_bytes() == b'original model'
    assert not lease.exists()
    assert fao.delete(path)


def test_missing_corrupt_truncated_and_decompression_bounds(tmp_path):
    path = tmp_path / 'required.json'
    with pytest.raises(FileNotFoundError):
        fao.read_bytes(path)
    fao.write_bytes(path, b'a' * 100000)
    with pytest.raises(fao.FileBudgetExceeded):
        fao.read_bytes(path, max_bytes=100)
    raw = path.read_bytes()
    # Keep valid custody while damaging the compressed stream.
    path.write_bytes(raw[:20] + raw[-fao.TRAILER.size:])
    with pytest.raises(fao.CorruptFile):
        fao.read_bytes(path)
    path.write_bytes(raw[:-1])
    with pytest.raises(fao.CorruptFile):
        fao.read_bytes(path)


def test_owner_retention_protects_references_and_records_expiry(tmp_path):
    path = tmp_path / 'diagnostic.json'
    fao.write_bytes(path, b'diagnostic')
    now, marked = time.time() + 181 * 86400, []
    for days in (7, 30, 180):
        for clock in ('ctime', 'mtime'):
            policy = fao.Retention(days, clock)
            assert not fao.expire(path, policy, now=now, required=lambda: True, expired=lambda: marked.append(True))
    assert not marked
    assert fao.expire(path, fao.Retention(30, 'ctime'), now=now, required=lambda: False,
                      expired=lambda: marked.append(True))
    assert marked == [True]
    assert not path.exists()


def test_immutable_retry_rejects_different_content(tmp_path):
    path = tmp_path / 'immutable'
    fao.write_bytes(path, b'first', immutable=True)
    fao.write_bytes(path, b'first', immutable=True)
    with pytest.raises(fao.CorruptFile):
        fao.write_bytes(path, b'other', immutable=True)
    assert fao.read_bytes(path) == b'first'


def test_read_work_is_bounded_across_repeated_seeks(tmp_path):
    path = tmp_path / 'pack'
    fao.write_bytes(path, b'x' * 100)
    with fao.open_reader(path, max_bytes=100, work_bytes=150) as stream:
        assert stream.read() == b'x' * 100
        stream.seek(0)
        with pytest.raises(fao.FileBudgetExceeded):
            stream.read()
