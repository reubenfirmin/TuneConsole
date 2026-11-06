"""All writes use the same compression and atomic replacement path."""
import pytest

from yt_playlist.core import fao


@pytest.mark.parametrize('staged', [False, True])
def test_publication_does_not_fsync_or_decompress_its_new_bytes(tmp_path, monkeypatch, staged):
    path = tmp_path / 'owner' / 'shard' / 'document'
    fao.write(path, b'previous')

    def forbidden(*args, **kwargs):
        pytest.fail('Write attempted fsync or a decompression read-back')

    with monkeypatch.context() as patch:
        patch.setattr(fao.os, 'fsync', forbidden)
        patch.setattr(fao._Reader, 'read', forbidden)
        if staged:
            with fao.prepare(path, b'new content') as pending:
                pending.publish()
        else:
            fao.write(path, b'new content')
    assert fao.read(path).bytes == b'new content'
    assert not list(path.parent.glob('.fao-pending-*'))
