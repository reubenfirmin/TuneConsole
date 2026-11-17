"""Cold originals remain exact without SQL receipts or the producer directory."""
from tests.file_custody_support import export_owner, restore_owner
import shutil

import pytest

from yt_playlist.core import fao, file_map, legacy_evidence, legacy_journals as owner
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store


def test_insert_once_rollback_snapshots_expiry_and_independent_restore(store, tmp_path):
    with atomic_write(store):
        legacy_evidence.retain(store.conn,'actions','operation',' exact signature ','unknown scope')
        legacy_evidence.retain_many(store.conn,'catalog', [('library',str(i),' {"old": %d} '%i,'conversion') for i in range(129)])
    assert store.conn.execute('SELECT count(*) FROM legacy_catalog_evidence').fetchone()[0] == 0
    with pytest.raises(RuntimeError):
        with atomic_write(store):
            legacy_evidence.retain(store.conn,'actions','rolled-back','uncommitted','recovery')
            raise RuntimeError('interrupted')
    assert legacy_evidence.original(store.conn,'workflow','actions','rolled-back') is None
    with store.read_view() as snapshot:
        before = owner.head(snapshot.conn,'catalog')
        for _ in range(40):
            with atomic_write(store):
                owner.maintain(store.conn,10**10)
        assert owner.head(snapshot.conn,'catalog') == before
        assert legacy_evidence.original(snapshot.conn,'catalog','library','128') == ' {"old": 128} '
        assert owner.collect(store.conn,10**10) == 0
    assert legacy_evidence.original(store.conn,'catalog','library','128') is None
    assert owner.read(store.conn,'catalog','library','128')['state'] == 'expired'
    with atomic_write(store):
        legacy_evidence.retain_many(store.conn,'catalog',[('library','128','replacement','retry')])
        legacy_evidence.retain(store.conn,'actions','operation','replacement','retry')
    assert legacy_evidence.original(store.conn,'catalog','library','128') is None
    assert legacy_evidence.original(store.conn,'workflow','actions','operation') == ' exact signature '
    manifest = export_owner(store.conn, owner,tmp_path/'saved')
    restored = Store(tmp_path/'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        restore_owner(restored.conn, 'legacy-journals', owner,manifest)
        restored.init_schema()
        assert legacy_evidence.original(restored.conn,'workflow','actions','operation') == ' exact signature '
        assert owner.read(restored.conn,'catalog','library','128')['state'] == 'expired'
        path = next(owner.references(restored.conn))
        fao.write(path, b'{}')
        with pytest.raises(fao.CorruptFile):
            legacy_evidence.original(restored.conn,'catalog','library','128')
    finally:
        restored.conn.close()


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
