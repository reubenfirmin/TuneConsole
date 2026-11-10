"""Disposable fixture recovery validates the whole inventory before installing."""
import pytest

from tests.file_custody_support import FileInventory
from yt_playlist.core import fao


@pytest.mark.parametrize('failure', ['content', 'missing', 'membership'])
def test_invalid_inventory_preserves_all_installed_files(tmp_path, failure):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    sources = [root / name for name in ('first.json', 'last.json')]
    targets = [restored / path.name for path in sources]
    for source, target in zip(sources, targets):
        fao.write(source, b'backup ' + source.name.encode())
        fao.write(target, b'last good ' + target.name.encode())
    before = [target.read_bytes() for target in targets]
    manifest = FileInventory.export(root, sources, backup)
    if failure == 'content':
        fao.write(backup / 'last.json', b'corrupt backup member')
    elif failure == 'missing':
        (backup / 'last.json').unlink()
    else:
        targets.append(restored / 'missing-owner.json')
    with pytest.raises((fao.CorruptFile, FileNotFoundError)):
        FileInventory.restore(restored, manifest, expected=targets)
    assert [target.read_bytes() for target in targets[:2]] == before
    assert not list(restored.glob('.fao-restore-*'))
    assert not list(restored.rglob('.fao-pending-*'))


@pytest.mark.parametrize('compressed', [False, True])
def test_verified_private_bytes_survive_backup_replacement(tmp_path, monkeypatch, compressed):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    source, target = root / 'one.json', restored / 'one.json'
    fao.write(source, b'original verified payload')
    clocks = fao.read(source).info
    manifest = FileInventory.export(root, [source], backup)
    original_digest = fao.Content.digest.fget
    changed = []

    def replace_after_verification(content):
        result = original_digest(content)
        if not changed:
            changed.append(True)
            if compressed:
                fao.write(backup / 'one.json', b'unchecked replacement')
            else:
                (backup / 'one.json').unlink()
                (backup / 'one.json').write_bytes(b'unchecked replacement')
        return result

    monkeypatch.setattr(fao.Content, 'digest', property(replace_after_verification))
    FileInventory.restore(restored, manifest, expected=[target])
    assert changed
    assert fao.read(target).bytes == b'original verified payload'
    copied = fao.read(target).info
    assert (copied.mtime_ns, copied.ctime_ns) == (clocks.mtime_ns, clocks.ctime_ns)
    assert not list(restored.glob('.fao-restore-*'))


def test_large_inventory_bounds_live_readers(tmp_path, monkeypatch):
    root, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    sources = [root / (str(i) + '.json') for i in range(40)]
    for source in sources:
        fao.write(source, source.name.encode())
    manifest = FileInventory.export(root, sources, backup)
    original_enter, original_exit = fao.Content.__enter__, fao.Content.__exit__
    active = maximum = 0

    def enter(content):
        nonlocal active, maximum
        reader = original_enter(content)
        active += 1
        maximum = max(maximum, active)
        return reader

    def leave(content, *exc):
        nonlocal active
        try:
            return original_exit(content, *exc)
        finally:
            active -= 1

    monkeypatch.setattr(fao.Content, '__enter__', enter)
    monkeypatch.setattr(fao.Content, '__exit__', leave)
    targets = [restored / path.name for path in sources]
    FileInventory.restore(restored, manifest, expected=targets)
    assert 0 < maximum <= 3 and active == 0
    assert [fao.read(path).bytes for path in targets] == [path.name.encode() for path in targets]
    assert not list(restored.glob('.fao-restore-*'))
