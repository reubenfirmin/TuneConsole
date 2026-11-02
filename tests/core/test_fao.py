"""Independent file contract checks, including old plain formats and failures."""
import bz2
from contextlib import contextmanager
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


def test_managed_copy_preserves_encoded_bytes_and_rejects_corruption_before_replace(tmp_path):
    source,target = tmp_path/'source',tmp_path/'backup'
    data = bytes(range(256))*1000
    fao.write_bytes(source,data)
    encoded,clocks = source.read_bytes(),fao.stat(source)
    fao.copy(source,target)
    assert target.read_bytes()==encoded
    assert fao.read_bytes(target)==data
    assert (fao.stat(target).mtime_ns,fao.stat(target).ctime_ns)==(clocks.mtime_ns,clocks.ctime_ns)
    broken = bytearray(encoded)
    broken[20]^=1
    source.write_bytes(broken)
    with pytest.raises(fao.CorruptFile):
        fao.copy(source,target)
    assert target.read_bytes()==encoded
    assert not list(tmp_path.glob('.fao-pending-*'))


@pytest.mark.parametrize('compressed', [False, True])
def test_copy_checks_domain_digest_before_replacing_a_valid_backup(tmp_path, compressed):
    source, target = tmp_path / 'source', tmp_path / 'backup'
    data = b'original domain document'
    if compressed:
        fao.write_bytes(source, data)
    else:
        source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    fao.copy(source, target, digest=digest)
    before = target.read_bytes()
    if compressed:
        fao.write_bytes(source, b'replaced valid managed document')
    else:
        source.write_bytes(b'replaced plain document')
    with pytest.raises(fao.CorruptFile, match='required content digest'):
        fao.copy(source, target, digest=digest)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob('.fao-pending-*'))


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


@pytest.mark.parametrize('compressed', [False, True])
def test_logical_touch_preserves_bytes_and_creation_age_and_is_bounded(tmp_path, compressed):
    path, data = tmp_path / 'context.json', b'{"shared":"original"}'
    if compressed:
        fao.write_bytes(path, data)
    else:
        path.write_bytes(data)
    before = fao.stat(path)
    fao.touch(path, mtime_ns=100*10**9)
    aged = fao.stat(path)
    assert aged.mtime_ns == 100*10**9
    assert aged.ctime_ns == before.ctime_ns
    assert fao.read_bytes(path) == data
    with pytest.raises(fao.FileBudgetExceeded):
        fao.touch(path, mtime_ns=200*10**9, max_bytes=len(data)-1)
    assert fao.stat(path) == aged
    fao.touch(path)
    renewed = fao.stat(path)
    assert renewed.mtime_ns >= before.mtime_ns
    assert renewed.ctime_ns == before.ctime_ns
    assert fao.read_bytes(path) == data


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


def test_backup_inventory_restore_and_corrupt_manifest_fail_before_install(tmp_path):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    path = root / 'owner' / '7.json'
    fao.write_bytes(path, b'original')
    manifest = fao.export(root, [path], backup)
    target = restored / 'owner' / '7.json'
    fao.restore(restored, manifest, expected=[target])
    assert fao.read_bytes(target) == b'original'
    assert fao.stat(target).ctime_ns == fao.stat(path).ctime_ns
    fao.write_bytes(backup / 'owner' / '7.json', b'changed')
    with pytest.raises(fao.CorruptFile):
        fao.restore(restored, manifest, expected=[target])
    assert fao.read_bytes(target) == b'original'


@pytest.mark.parametrize('compressed', [False, True])
def test_restore_publishes_private_verified_bytes_after_backup_replacement(tmp_path, monkeypatch, compressed):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    source, target = root/'one.json', restored/'one.json'
    fao.write_bytes(source, b'original verified payload')
    clocks = fao.stat(source)
    manifest = fao.export(root, [source], backup)
    fao.write_bytes(target, b'last good')
    original_hash = fao._hash
    changed = []
    def replace_after_verification(path):
        result = original_hash(path)
        if not changed:
            changed.append(True)
            if compressed:
                fao.write_bytes(backup/'one.json', b'unchecked replacement')
            else:
                (backup/'one.json').unlink()
                (backup/'one.json').write_bytes(b'unchecked replacement')
        return result
    monkeypatch.setattr(fao, '_hash', replace_after_verification)
    fao.restore(restored, manifest, expected=[target])
    assert changed
    assert fao.read_bytes(target) == b'original verified payload'
    assert fao.stat(target).mtime_ns == clocks.mtime_ns
    assert fao.stat(target).ctime_ns == clocks.ctime_ns
    assert not list(restored.glob('.fao-pending-*'))
    assert not list(restored.glob('.fao-restore-*'))


@pytest.mark.parametrize('failure', ['content', 'missing', 'membership'])
def test_restore_rejects_entire_inventory_and_cleans_private_bytes(tmp_path, failure):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    sources = [root / name for name in ('first.json', 'last.json')]
    targets = [restored / path.name for path in sources]
    for source, target in zip(sources, targets):
        fao.write_bytes(source, b'backup ' + source.name.encode())
        fao.write_bytes(target, b'last good ' + target.name.encode())
    before = [target.read_bytes() for target in targets]
    manifest = fao.export(root, sources, backup)
    if failure == 'content':
        fao.write_bytes(backup / 'last.json', b'corrupt backup member')
    elif failure == 'missing':
        (backup / 'last.json').unlink()
    else:
        targets.append(restored / 'missing-owner.json')
    with pytest.raises((fao.CorruptFile, FileNotFoundError)):
        fao.restore(restored, manifest, expected=targets)
    assert [target.read_bytes() for target in targets[:2]] == before
    assert not list(restored.glob('.fao-restore-*'))
    assert not list(restored.rglob('.fao-pending-*'))


def test_restore_bounds_open_streams_across_large_inventory(tmp_path, monkeypatch):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    sources = [root / (str(i) + '.json') for i in range(40)]
    for source in sources:
        fao.write_bytes(source, source.name.encode())
    manifest = fao.export(root, sources, backup)
    original = fao.open_reader
    active = maximum = 0

    @contextmanager
    def counted(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            with original(*args, **kwargs) as reader:
                yield reader
        finally:
            active -= 1

    monkeypatch.setattr(fao, 'open_reader', counted)
    targets = [restored / path.name for path in sources]
    fao.restore(restored, manifest, expected=targets)
    assert maximum <= 3 and active == 0
    assert [fao.read_bytes(path) for path in targets] == [path.name.encode() for path in targets]
    assert not list(restored.glob('.fao-restore-*'))


def test_export_manifest_matches_copied_bytes_when_producer_is_replaced(tmp_path, monkeypatch):
    root, backup = tmp_path/'source', tmp_path/'backup'
    source = root/'one.json'
    fao.write_bytes(source, b'old publication')
    original_copy = fao.copy
    def replace_before_copy(source, target, **kwargs):
        fao.write_bytes(source, b'new publication')
        return original_copy(source, target, **kwargs)
    monkeypatch.setattr(fao, 'copy', replace_before_copy)
    manifest = fao.export(root, [source], backup)
    restored = tmp_path/'restored'
    fao.restore(restored, manifest, expected=[restored/'one.json'])
    assert fao.read_bytes(restored/'one.json') == b'new publication'


def test_http_original_type_ranges_cache_and_conversion(tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient
    from yt_playlist.web.fao_static import ManagedStaticFiles

    path = tmp_path / 'image.png'
    data = bytes(range(256)) * 100
    path.write_bytes(data)
    app = Starlette(routes=[Mount('/assets', ManagedStaticFiles(directory=tmp_path))])
    with TestClient(app) as client:
        original = client.get('/assets/image.png')
        fao.convert(path)
        compressed = client.get('/assets/image.png')
        assert compressed.content == original.content == data
        assert compressed.headers['content-type'] == 'image/png'
        assert compressed.headers['content-length'] == str(len(data))
        assert compressed.headers['etag'] == original.headers['etag']
        assert client.get('/assets/image.png', headers={'If-None-Match': compressed.headers['etag']}).status_code == 304
        selected = client.get('/assets/image.png', headers={'Range': 'bytes=100-199'})
        assert selected.status_code == 206 and selected.content == data[100:200]
        assert selected.headers['content-range'] == f'bytes 100-199/{len(data)}'
