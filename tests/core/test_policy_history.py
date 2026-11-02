"""Historical attribution survives body expiry, rollback and independent restore."""
import shutil

import pytest

from yt_playlist.core import fao, policy_history as owner, policy_documents
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import policy_history
from yt_playlist.core.store import Store
from tests.rec.test_policy_storage import _publish


def test_impression_only_history_keeps_exact_shared_configuration_and_compact_identity(store, tmp_path, monkeypatch):
    rows,base = _publish(store,1,('history','operational'),{'weights':{'rock':1.23456789},'original':'retain exactly'})
    historical,live = rows[0][0],rows[1][0]
    store.rec_ledger.record_served_impressions('old','home',[{'key':'song','rank':1,
        'model_version':historical,'provenance':{'pool_policy':historical}}],10000)
    store.rec_proposals.put_proposals('home_choice',{'items':[{'pool_policy':live}]},10000)
    expected = store.rec_ledger.policy_configuration(historical)
    with monkeypatch.context() as patch:
        patch.setattr(fao,'write_bytes',lambda *a,**k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError,match='disk full'):
            policy_history.prepare(store,now=10000)
    assert store.rec_ledger.policy_configuration(historical) == expected
    with store.read_view() as snapshot:
        assert policy_history.prepare(store,now=10000) == 1
        assert policy_documents.configuration(snapshot.conn,historical) == expected
        assert policy_documents.collect_orphans(store.conn) == 0
    assert store.rec_ledger.policy_configuration(historical) == expected
    assert not store.conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?',(historical,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?',(live,)).fetchone()
    assert store.conn.execute('SELECT count(*) FROM historical_impression_policies').fetchone()[0] == 2
    assert owner.document(store.conn,historical)['base']['version'] == base[0]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    manifest = owner.export(store.conn,tmp_path/'saved')
    restored = Store(tmp_path/'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        owner.restore(restored.conn,manifest)
        restored.init_schema()
        assert restored.rec_ledger.policy_configuration(historical) == expected
        with atomic_write(restored):
            assert owner.expire(restored.conn,181*86400) == 1
        assert restored.rec_ledger.policy_configuration(historical) is None
        assert restored.conn.execute('SELECT state FROM recommendation_policy_history').fetchone()[0] == 'expired'
        assert restored.conn.execute('SELECT count(*) FROM historical_impression_policies').fetchone()[0] == 2
        # Expired diagnostics never remove the live prepared policy or its base.
        assert restored.conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?',(live,)).fetchone()
    finally:
        restored.conn.close()


def test_missing_required_history_is_a_failure_and_queued_policy_can_republish(store):
    store.rec_ledger.record_serving_policy('old',{'exact':True},1)
    capture = policy_documents.captured(store.conn,['old'])
    store.rec_ledger.record_served_impressions('old','home',[{'key':'song','rank':1,'model_version':'old'}],10000)
    assert policy_history.prepare(store,now=10000) == 1
    store.rec_ledger.record_served_impressions('later','home',[{'key':'song','rank':1,'model_version':'old'}],10001)
    assert store.conn.execute('SELECT count(*) FROM historical_impression_policies').fetchone()[0]==2
    assert not store.conn.execute("SELECT 1 FROM recommendation_impression_policies WHERE raw_version='old'").fetchone()
    with atomic_write(store):
        policy_documents.install(store.conn,*capture)
    assert store.rec_ledger.policy_configuration('old') == {'exact':True}
    assert policy_history.prepare(store,now=10000) == 0  # Old attribution already has its compact owner.
    with atomic_write(store):
        store.conn.execute("DELETE FROM recommendation_policies WHERE version='old'")
    path = next(owner.references(store.conn))
    fao.write_bytes(path,b'{}')
    with pytest.raises(fao.CorruptFile):
        store.rec_ledger.policy_configuration('old')


def test_large_configuration_cohort_uses_byte_bounded_batches_and_converges(store,monkeypatch):
    from yt_playlist.core import file_map
    monkeypatch.setattr(file_map,'MAX_BYTES',8*1024)
    monkeypatch.setattr(file_map,'MAX_WORK',32*1024)
    configs = {f'history-{i}':{'original':str(i),'body':'configuration '+str(i)+'x'*2100} for i in range(12)}
    for version,config in configs.items():
        store.rec_ledger.record_serving_policy(version,config,1)
        store.rec_ledger.record_served_impressions(version,'home',[
            {'key':'song','rank':1,'model_version':version}],10000)
    counts = []
    while count := policy_history.prepare(store,now=10000):
        counts.append(count)
        assert len(counts)<=len(configs)
    assert sum(counts)==len(configs)
    assert max(counts)<len(configs)
    assert all(store.rec_ledger.policy_configuration(version)==config for version,config in configs.items())
    assert store.conn.execute('SELECT count(*) FROM historical_impression_policies').fetchone()[0]==len(configs)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
