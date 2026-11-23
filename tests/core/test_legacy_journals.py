"""Retired archive files respect read leases and the collection grace period."""
from types import SimpleNamespace

import pytest


from yt_playlist.controllers.managed_file_retention import ManagedFileRetention
from yt_playlist.core import fao, file_map, policy_documents


def test_retired_archive_pages_wait_for_the_reader_and_grace_period(tmp_path):
    root = tmp_path / 'state.db.legacy-journals'
    publication = file_map.update(root / 'artist', None, {'old': {'diagnostic': True}})
    page = root / 'artist' / file_map.relative(publication)
    unrelated = root / 'artist' / page.parent.name / 'keep.json'
    fao.write(unrelated, b'{}')
    collector = ManagedFileRetention(SimpleNamespace(), None, [(root, 'retired-archive-journals')], [])
    now = fao.read(page).info.ctime_ns / 1e9
    try:
        assert collector.maintain(now) == 0
        with policy_documents.retained_reader(root):
            assert collector.maintain(now + 31 * 86400) == 0
            assert page.exists()
        assert collector.maintain(now + 31 * 86400) == 1
        assert not page.exists()
        assert unrelated.exists()
    finally:
        collector.close()


def test_trie_seek_is_bounded_and_missing_required_pages_fail(tmp_path, monkeypatch):
    root = tmp_path/'map'
    publication = None
    for start in range(0,512,128):
        publication = file_map.update(root,publication,{str(i):{'value':i} for i in range(start,start+128)})
    calls, read = [], file_map._read
    def counted(*args,**kwargs):
        calls.append(args[1])
        return read(*args,**kwargs)
    monkeypatch.setattr(file_map,'_read',counted)
    after, seen = None, {}
    while True:
        before = len(calls)
        leaf = file_map.next_leaf(root,publication,after)
        assert len(calls)-before <= 3
        if leaf is None:
            break
        after,page = leaf
        seen.update(page['entries'])
    assert seen == {str(i):{'value':i} for i in range(512)}
    assert file_map.get(root,publication,'511') == {'value':511}
    fao.delete(root/file_map.relative(publication))
    with pytest.raises(FileNotFoundError):
        file_map.get(root,publication,'511')
