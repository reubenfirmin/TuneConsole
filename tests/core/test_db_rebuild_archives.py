"""Stable archive work ordering preserves domain eligibility and atomic resume."""
import hashlib

import pytest

from yt_playlist.core import file_map, legacy_evidence, legacy_journals, policy_history
from yt_playlist.core.db_rebuild import archives
from yt_playlist.core.db_writes import atomic_write


def test_archive_order_resumes_partial_byte_batches_without_changing_originals(store, monkeypatch):
    monkeypatch.setattr(file_map,'MAX_BYTES',8*1024)
    monkeypatch.setattr(file_map,'MAX_WORK',32*1024)
    expected = {f'history-{i}': {'original':str(i),'body':str(i)+'x'*2100} for i in range(12)}
    for version,configuration in expected.items():
        store.rec_ledger.record_serving_policy(version,configuration,1)
        store.rec_ledger.record_served_impressions(version,'home',[
            {'key':'song','rank':1,'model_version':version}],10000)
    store.rec_ledger.record_serving_policy('operational',{'live':True},1)
    store.rec_ledger.record_served_impressions('live','home',[
        {'key':'song','rank':1,'model_version':'operational'}],10000)
    store.rec_proposals.put_proposals('home_choice',{'tracks':[{'pool_policy':'operational'}]},10000)
    with atomic_write(store):
        legacy_evidence.stage_many(store.conn,'album',[
            ('album_tracklists','unicode-ñ',' {"tracks": [1, 2]} ','original conversion'),
            ('paid','retained',' exact paid original ','paid obligation')])
    stages = archives.stages(10000)
    with atomic_write(store):
        assert stages[0].copy(None,store.conn,None,128).rows==14
    queued = [row[0] for row in store.conn.execute('SELECT identity FROM '+archives.WORK+
        " WHERE owner='policies' ORDER BY route,identity")]
    assert queued==sorted(expected,key=lambda value:hashlib.sha256(value.encode()).hexdigest())
    policies = next(stage for stage in stages if stage.name=='archives:policies')
    before = policy_history.head(store.conn)
    with pytest.raises(InterruptedError):
        with atomic_write(store):
            assert policies.copy(None,store.conn,None,128).rows>0
            raise InterruptedError('before bookmark commit')
    assert policy_history.head(store.conn)==before
    assert store.conn.execute('SELECT count(*) FROM '+archives.WORK).fetchone()[0]==14
    counts = []
    for stage in stages[1:]:
        cursor = None
        while True:
            with atomic_write(store):
                batch = stage.copy(None,store.conn,cursor,128)
            if stage==policies and batch.rows:
                counts.append(batch.rows)
            if batch.done:
                break
            cursor = batch.cursor
    assert sum(counts)==len(expected) and max(counts)<len(expected)
    assert all(store.rec_ledger.policy_configuration(version)==value for version,value in expected.items())
    assert store.rec_ledger.policy_configuration('operational')=={'live':True}
    assert legacy_evidence.original(store.conn,'album','album_tracklists','unicode-ñ')==' {"tracks": [1, 2]} '
    assert legacy_evidence.original(store.conn,'album','paid','retained')==' exact paid original '
    assert not store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(archives.WORK,)).fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_selected_policy_conversion_rechecks_operational_protection(store):
    store.rec_ledger.record_serving_policy('live',{'original':True},1)
    store.rec_ledger.record_served_impressions('live','home',[
        {'key':'song','rank':1,'model_version':'live'}],10000)
    store.rec_proposals.put_proposals('home_choice',{'tracks':[{'pool_policy':'live'}]},10000)
    with atomic_write(store):
        assert policy_history.prepare_batch(store.conn,10000,versions=['live'])==0
    assert store.rec_ledger.policy_configuration('live')=={'original':True}
    assert store.conn.execute("SELECT count(*) FROM recommendation_policies WHERE version='live'").fetchone()[0]==1


def test_staged_originals_keep_first_capture_and_rollback_with_the_import(store):
    with atomic_write(store):
        legacy_evidence.retain_many(store.conn, 'album', [('paid', 'published', 'first published', 'paid original')])
        legacy_evidence.stage_many(store.conn, 'album', [
            ('paid', 'staged', 'first staged', 'paid original'),
            ('paid', 'staged', 'duplicate in the same batch', 'later capture')])
    publication = legacy_journals.head(store.conn, 'album')
    with atomic_write(store):
        legacy_evidence.stage_many(store.conn, 'album', [
            ('paid', 'published', 'replacement', 'later capture'),
            ('paid', 'staged', 'replacement', 'later capture')])
    assert legacy_journals.head(store.conn, 'album') == publication
    assert legacy_evidence.original(store.conn, 'album', 'paid', 'published') == 'first published'
    assert legacy_evidence.original(store.conn, 'album', 'paid', 'staged') == 'first staged'
    with pytest.raises(InterruptedError):
        with atomic_write(store):
            legacy_evidence.stage_many(store.conn, 'album', [('paid', 'interrupted', 'exact input', 'paid original')])
            raise InterruptedError('import transaction failed')
    assert legacy_evidence.original(store.conn, 'album', 'paid', 'interrupted') is None
    assert store.conn.execute('SELECT count(*) FROM album_identity_evidence').fetchone()[0] == 1
