"""Useful file behavior through the read/write/prepare/delete interface."""
import bz2
import hashlib
from pathlib import Path

import pytest

from yt_playlist.core import fao


@pytest.mark.parametrize('data', [b'', bytes(range(256)), ('été 東京 ' * 10000).encode()])
def test_exact_round_trip_and_bounded_stream(tmp_path, data):
    path = tmp_path / 'owner' / 'content.bin'
    assert fao.write(path, data) == path
    assert bz2.decompress(path.read_bytes()) == data
    assert fao.read(path).bytes == data
    content = fao.read(path)
    assert content.info.logical_bytes == len(data)
    assert content.digest == (len(data), hashlib.sha256(data).hexdigest())
    with content as stream:
        assert stream.read(3) + stream.read() == data


def test_installed_plain_and_compressed_files_remain_readable_and_writable(tmp_path):
    path = tmp_path / 'installed.json'
    data = b'{"original": 1.000001}\n'
    path.write_bytes(data)
    content = fao.read(path)
    before = content.info
    assert content.bytes == data
    fao.write(path, content)
    after = fao.read(path).info
    assert after.compressed and after.logical_bytes == len(data)
    assert (after.mtime_ns, after.ctime_ns) == (before.mtime_ns, before.ctime_ns)
    fao.write(path, b'continuing write')
    assert fao.read(path).bytes == b'continuing write'


def test_read_copy_validates_source_before_replacing_target(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    fao.write(source, b'verified source')
    fao.write(target, b'last good')
    info = fao.read(source).info
    fao.write(target, fao.read(source, digest=hashlib.sha256(b'verified source').hexdigest()))
    assert fao.read(target).bytes == b'verified source'
    assert fao.read(target).info.ctime_ns == info.ctime_ns
    with pytest.raises(fao.CorruptFile, match='digest'):
        fao.write(target, fao.read(source, digest='0' * 64))
    raw = source.read_bytes()
    source.write_bytes(raw[:20] + raw[-fao.TRAILER.size:])
    with pytest.raises(fao.CorruptFile):
        fao.write(target, fao.read(source))
    assert fao.read(target).bytes == b'verified source'
    assert not list(tmp_path.glob('.fao-pending-*'))


def test_prepare_is_private_until_publish_and_always_cleans_up(tmp_path):
    path = tmp_path / 'document'
    fao.write(path, b'previous')
    with fao.prepare(path, b'discarded') as stage:
        assert fao.read(path).bytes == b'previous'
        assert stage.pending.exists()
    assert not stage.pending
    assert fao.read(path).bytes == b'previous'
    with fao.prepare(path, b'published') as stage:
        stage.publish()
        with pytest.raises(RuntimeError, match='consumed'):
            stage.publish()
    assert fao.read(path).bytes == b'published'
    assert not list(tmp_path.glob('.fao-pending-*'))


def test_stale_preparation_cannot_replace_new_publication(tmp_path):
    path = tmp_path / 'document'
    with fao.prepare(path, b'stale') as stage:
        fao.write(path, b'new publication')
        with pytest.raises(fao.FileChanged):
            stage.publish()
    assert fao.read(path).bytes == b'new publication'
    old = fao.read(path)
    old.bytes
    fao.write(path, b'newer publication')
    with pytest.raises(fao.FileChanged):
        fao.write(path, old.replacing(b'stale edit'))
    assert fao.read(path).bytes == b'newer publication'


def test_prepared_publication_remains_readable_until_the_writer_context_closes(tmp_path):
    path = tmp_path / 'document'
    with fao.prepare(path, b'awaiting SQL reference') as stage:
        stage.publish()
        assert not fao.delete(path)
        assert fao.read(path).bytes == b'awaiting SQL reference'
    assert fao.delete(path)


@pytest.mark.parametrize('failure', ['compress', 'replace'])
def test_write_failure_preserves_last_publication_and_removes_pending(tmp_path, monkeypatch, failure):
    path = tmp_path / 'document'
    fao.write(path, b'last good')

    def interrupted(*args, **kwargs):
        raise OSError('interrupted publication')

    with monkeypatch.context() as patch:
        if failure == 'compress':
            patch.setattr(fao.bz2, 'BZ2Compressor', interrupted)
        else:
            patch.setattr(fao.os, 'replace', interrupted)
        with pytest.raises(OSError, match='interrupted publication'):
            fao.write(path, b'replacement')
    assert fao.read(path).bytes == b'last good'
    assert not list(tmp_path.glob('.fao-pending-*'))


def test_missing_corrupt_truncated_and_decompression_bounds(tmp_path):
    path = tmp_path / 'required'
    with pytest.raises(FileNotFoundError):
        fao.read(path).bytes
    fao.write(path, b'a' * 100000)
    with pytest.raises(fao.FileBudgetExceeded):
        fao.read(path, max_bytes=100).bytes
    with pytest.raises(fao.FileBudgetExceeded):
        fao.write(path, fao.Content(b'oversized', max_bytes=3))
    assert fao.read(path).bytes == b'a' * 100000
    raw = path.read_bytes()
    path.write_bytes(raw[:20] + raw[-fao.TRAILER.size:])
    with pytest.raises(fao.CorruptFile):
        fao.read(path).bytes
    path.write_bytes(raw[:-1])
    with pytest.raises(fao.CorruptFile):
        fao.read(path).bytes
    # An ordinary write can repair a damaged prior encoding.
    fao.write(path, b'repaired')
    assert fao.read(path).bytes == b'repaired'


def test_read_work_is_bounded_across_repeated_seeks(tmp_path):
    path = tmp_path / 'pack'
    fao.write(path, b'x' * 100)
    with fao.read(path, max_bytes=100, work_bytes=150) as stream:
        assert stream.read() == b'x' * 100
        stream.seek(0)
        with pytest.raises(fao.FileBudgetExceeded):
            stream.read()


@pytest.mark.parametrize('access', ['bytes', 'digest', 'materialize', 'stream'])
def test_all_read_forms_enforce_content_digest_and_bounds(tmp_path, access):
    path = tmp_path / 'document'
    fao.write(path, b'bounded content')

    def consume(content):
        if access == 'materialize':
            with content.materialize() as decoded:
                return decoded.read_bytes()
        if access == 'stream':
            with content as stream:
                return stream.read()
        return getattr(content, access)

    with pytest.raises(fao.CorruptFile, match='digest'):
        consume(fao.read(path, digest='0' * 64))
    with pytest.raises(fao.FileBudgetExceeded):
        consume(fao.read(path, max_bytes=3))


def test_materialized_content_pins_original_until_release(tmp_path):
    path = tmp_path / 'model.bin'
    fao.write(path, b'original model')
    with fao.read(path).materialize() as lease:
        assert lease.read_bytes() == b'original model'
        assert not fao.delete(path)
        fao.write(path, b'updated model')
        assert lease.read_bytes() == b'original model'
    assert not lease.exists()
    assert fao.delete(path)
    assert not fao.delete(path)


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
        fao.write(path, fao.read(path))
        compressed = client.get('/assets/image.png')
        assert compressed.content == original.content == data
        assert compressed.headers['content-type'] == 'image/png'
        assert compressed.headers['content-length'] == str(len(data))
        assert compressed.headers['etag'] == original.headers['etag']
        assert client.get('/assets/image.png', headers={'If-None-Match': compressed.headers['etag']}).status_code == 304
        selected = client.get('/assets/image.png', headers={'Range': 'bytes=100-199'})
        assert selected.status_code == 206 and selected.content == data[100:200]
        assert selected.headers['content-range'] == f'bytes 100-199/{len(data)}'
