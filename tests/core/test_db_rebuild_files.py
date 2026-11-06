"""File custody survives interruption between individual directory renames."""
import json

import pytest

from yt_playlist.core import fao
from yt_playlist.core.db_rebuild import files


class ProcessDied(BaseException):
    pass


def test_private_databases_reuse_installed_file_owners_without_copying(tmp_path):
    source, candidate, original = (tmp_path/name for name in ('state.db', 'copy.db', 'original.db'))
    installed = files.roots(source)
    before = {}
    for domain, root in installed.items():
        path = root/'retained.json'
        fao.write_bytes(path, b'original '+domain.encode())
        before[domain] = path.stat(), path.read_bytes()
    for database in (candidate, original):
        files.borrow(source, database)
        files.borrow(source, database)
        for domain, root in files.roots(database).items():
            retained = root/'retained.json'
            assert retained.samefile(installed[domain]/'retained.json')
            info, encoded = before[domain]
            assert retained.stat().st_ino == info.st_ino
            assert retained.read_bytes() == encoded
    publication = files.roots(candidate)['runtime_metadata']/'new.json'
    fao.write_bytes(publication, b'new runtime document')
    assert (installed['runtime_metadata']/'new.json').samefile(publication)


@pytest.mark.parametrize('during', ['install','rollback'])
@pytest.mark.parametrize('after', range(1,7))
def test_interrupted_root_moves_restore_every_original_byte(tmp_path,monkeypatch,during,after):
    database, original, candidate = (tmp_path/name for name in ('state.db','original.db','candidate.db'))
    installed, prepared = files.roots(database), files.roots(candidate)
    originals = {}
    for domain in ('vibe-provenance','legacy-journals','profiles'):
        path = installed[domain]/'retained.json'
        fao.write_bytes(path, b'original '+domain.encode())
        originals[path] = path.read_bytes()
    for domain in ('vibe-provenance','legacy-journals','served'):
        fao.write_bytes(prepared[domain]/'retained.json', b'candidate '+domain.encode())
    # Persisted state from the older root-moving installer remains supported.
    saved = files.roots(original)
    state = json.loads(json.dumps({'file_roots': [
        {'domain': domain, 'installed': str(installed[domain]), 'original': str(saved[domain]),
         'candidate': str(prepared[domain]), 'had_original': domain != 'served',
         'has_candidate': domain != 'profiles'}
        for domain in ('vibe-provenance', 'legacy-journals', 'profiles', 'served')]}))
    if during == 'rollback':
        files.install(state,progress=lambda *_:None)
    move, count = files._move, 0
    def interrupted(source,target):
        nonlocal count
        move(source,target)
        count += 1
        if count == after:
            raise ProcessDied()
    with monkeypatch.context() as patch:
        patch.setattr(files,'_move',interrupted)
        with pytest.raises(ProcessDied):
            if during == 'install':
                files.install(state,progress=lambda *_:None)
            else:
                files.rollback(state)
    files.rollback(state)
    files.rollback(state)
    assert all(path.read_bytes()==encoded for path,encoded in originals.items())
    assert not installed['served'].exists()
    assert not any(root.exists() for root in files.roots(original).values())
