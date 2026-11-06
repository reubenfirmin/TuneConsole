"""Cache writes compress and replace; round-trip verification belongs here."""
import bz2

import pytest

from yt_playlist.core import fao


@pytest.mark.parametrize('body', [b'', bytes(range(256)), ('été 東京 ' * 10000).encode()])
def test_compressed_write_round_trips_without_syncs_or_rereading(tmp_path, monkeypatch, body):
    path = tmp_path / 'business-chosen' / 'menu.cache'

    def forbidden(*args, **kwargs):
        pytest.fail('Cache write attempted a durability protocol or read-back')

    with monkeypatch.context() as patch:
        patch.setattr(fao.os, 'fsync', forbidden)
        patch.setattr(fao, 'open_reader', forbidden)
        patch.setattr(fao, '_publication', forbidden)
        assert fao.write_compressed(path, body) == path
    assert bz2.decompress(path.read_bytes()) == body
    assert fao.read_bytes(path) == body
    assert not list(path.parent.glob('.fao-*'))


def test_failed_replace_keeps_the_previous_cache_and_removes_pending_file(tmp_path, monkeypatch):
    path = tmp_path / 'menu.cache'
    fao.write_compressed(path, b'previous menu')

    def interrupted(*args):
        raise OSError('replacement failed')

    monkeypatch.setattr(fao.os, 'replace', interrupted)
    with pytest.raises(OSError, match='replacement failed'):
        fao.write_compressed(path, b'replacement menu')
    assert fao.read_bytes(path) == b'previous menu'
    assert list(tmp_path.iterdir()) == [path]
