"""Explicit action ownership survives supported legacy formats and lost workers."""
from contextlib import closing
import json
import sqlite3

import pytest

from yt_playlist.core import action_backup_files, collection_recovery, fao, playlist_scopes
from yt_playlist.core import workflow_settings
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Action ownership checks drive local collaborators without the HTTP app."""


def backup(tmp_path, account, remote):
    path = tmp_path / (remote + '.json')
    path.write_text(json.dumps({'snapshot_version': 2, 'identity_id': account,
        'ytm_playlist_id': remote, 'title': remote,
        'tracks': [{'video_id': 'one'}, {'video_id': 'one'}]}))
    return str(path)


def test_expired_claim_requires_reconciliation_and_fences_stale_completion(store,tmp_path):
    account = store.accounts.upsert_account('Owner',None,True)
    store.playlists.upsert_playlist(account,'SOURCE','Source',2,'',1)
    undo = {'deletions':[{'identity':account,'ytm':'SOURCE','phase':'pending','backup':backup(tmp_path,account,'SOURCE')}]}
    action = store.actions.record_action('delete_playlist','{}','{}','delete_pending',json.dumps(undo),2)
    target, first = store.actions.begin_deletion(action, account, 'SOURCE', 3)
    with pytest.raises(ValueError,match='Another action worker'):
        store.actions.begin_deletion(action, account, 'SOURCE', 4, reconcile=True)
    competing = store.actions.record_action('copy_playlist', '{}', '{}', 'executed',
        json.dumps({'new_ytm': 'SOURCE', 'target_identity': account}), 5)
    with pytest.raises(ValueError,match='outcome is unknown'):
        store.actions.claim_forward_targets(competing, 'destination', 64)
    _, replacement = store.actions.begin_deletion(action, account, 'SOURCE', 64, reconcile=True)
    with pytest.raises(ValueError,match='stale worker'):
        store.actions.repository.require_claim(target, first)
    store.actions.release_deletion((target, first))
    store.actions.repository.require_claim(target, replacement)


def test_idempotent_intent_and_target_agreement_are_enforced(store):
    account = store.accounts.upsert_account('Owner',None,True)
    other = store.accounts.upsert_account('Other',None,False)
    membership = store.playlists.upsert_playlist(account,'LM','Liked',1,'',1)
    plan = json.dumps({'playlist_id':membership,'after_video_id':'anchor','video_ids':['one','one'],
                       'set_video_ids':{'one':'exact-occurrence'}})
    first = store.actions.record_action('add_tracks','{}',plan,'pending','{}',2,idempotency_key='request')
    assert store.actions.record_action('add_tracks','{}',plan,'pending','{}',2,idempotency_key='request')==first
    with pytest.raises(ValueError,match='another action intent'):
        store.actions.record_action('delete_playlist','{}','{}','planned','{}',2,idempotency_key='request')
    assert json.loads(store.actions.get_action(first).plan_json)==json.loads(plan)
    target = store.conn.execute('SELECT * FROM action_targets WHERE action_id=?',(first,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError,match='disagrees'):
        with store.conn:
            store.conn.execute('INSERT INTO action_targets(action_id,position,role,account_id,playlist_scope_id,'
                               'provider_playlist_id_snapshot) VALUES (?,1,?,?,?,?)',
                               (first,'deletion',other,target['playlist_scope_id'],'LM'))
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        with store.conn:
            store.conn.execute('UPDATE action_targets SET account_id=? WHERE id=?',(other,target['id']))


def test_recovery_uses_existing_local_adapter_and_keeps_one_undo_link(store,tmp_path,monkeypatch):
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from yt_playlist.library import playlist_undo
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    client = RecoveryClient({'SOURCE':['one','one','two']})
    account,action = _deleted(store,client)
    client.lose_create = True
    client.lose_add = True
    playlist_undo.undo_action(store,action,{account:client},3)
    assert store.actions.get_action(action).status=='undone'
    assert [row['videoId'] for row in client._tracks['PL_NEW_0']]==['one','one','two']
    assert len(client.created)==1
    assert store.conn.execute('SELECT count(*) FROM actions WHERE undo_of_action_id=?',(action,)).fetchone()[0]==1


def test_reviewed_draft_is_frozen_before_merge_and_stale_review_cannot_write(store,tmp_path,monkeypatch):
    from tests.test_undo_recovery import RecoveryClient
    from yt_playlist.library.executor import apply_result
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    client = RecoveryClient({'FIRST':['one','two'],'SECOND':['one']})
    account = store.accounts.upsert_account('Owner',None,True)
    memberships = [store.playlists.upsert_playlist(account,remote,remote,2,'',1) for remote in ('FIRST','SECOND')]
    draft_id = store.actions.record_action('merge_draft','{"keep":"all"}','FIRST|SECOND','draft','opaque',1)
    stale = store.actions.get_action(draft_id)
    store.actions.update_draft(draft_id,'{"keep":"all","edited":true}')
    with pytest.raises(ValueError,match='draft changed'):
        apply_result(store,{account:client},memberships,['one'],'all',2,reviewed_draft=(draft_id,stale.revision))
    assert not client.added and not client.removed and not client.deleted
    draft = store.actions.get_action(draft_id)
    remove = client.remove_playlist_items
    def guarded_remove(remote,tracks):
        step = store.conn.execute("SELECT s.*,a.reviewed_draft_revision,t.claim_token AS target_claim FROM action_steps s JOIN actions a ON a.id=s.action_id "
            "JOIN action_targets t ON t.id=s.target_id WHERE s.operation='merge_plan' AND t.provider_playlist_id_snapshot=?",
            (remote,)).fetchone()
        assert step['phase']=='uncertain' and step['outcome']=='unknown'
        assert step['reviewed_draft_revision']==draft.revision
        assert step['target_claim']
        store.actions.backups.reference(step['backup_filename'])
        return remove(remote,tracks)
    monkeypatch.setattr(client,'remove_playlist_items',guarded_remove)
    apply_result(store,{account:client},memberships,['one'],'all',2,reviewed_draft=(draft_id,draft.revision))
    row = store.conn.execute("SELECT * FROM actions WHERE kind='apply_merge'").fetchone()
    captured = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='action',owner=row['id'])
    assert captured['reviewed_draft']['params']=='{"keep":"all","edited":true}'
    store.actions.delete_action(draft_id)
    assert store.actions.get_action(row['id']) is not None
    assert store.conn.execute('SELECT count(*) FROM action_targets WHERE claim_token IS NOT NULL').fetchone()[0]==0


def test_export_retains_backup_bytes_after_original_file_is_removed(store,tmp_path,monkeypatch):
    from pathlib import Path
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from yt_playlist.core import action_backup_files, fao
    from yt_playlist.library.playlist_undo import undo_action
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    client = RecoveryClient({'SOURCE':['one','one','two']})
    account,action = _deleted(store,client)
    reference, = store.actions.repository.backups(action)
    original, retained = reference['filename'], reference['retained_filename']
    assert action_backup_files.record_id(retained) is not None
    assert fao.read(original).bytes == fao.read(retained).bytes
    store.conn.execute('BEGIN')
    manifest = action_backup_files.export(store.conn,tmp_path/'export')
    store.conn.rollback()
    Path(original).unlink()
    Path(retained).unlink()
    action_backup_files.restore(store.conn,manifest)
    assert store.actions.repository.backups(action)[0]['retained_filename'] == retained
    undo_action(store,action,{account:client},3)
    assert store.actions.get_action(action).status=='undone'
    assert [row['videoId'] for row in client._tracks['PL_NEW_0']]==['one','one','two']


def test_unknown_copy_deletion_reconciles_without_replaying_write(store,tmp_path,monkeypatch):
    from tests.test_undo_recovery import RecoveryClient, _copy
    from yt_playlist.library.playlist_undo import undo_action
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    client = RecoveryClient({'COPY':['one','two']})
    account,action = _copy(store,client)
    attempts = []
    def unknown(remote):
        attempts.append(remote)
        raise TimeoutError('No outcome was received')
    monkeypatch.setattr(client,'delete_playlist',unknown)
    for now in (2,3):
        with pytest.raises(ValueError,match='Undo incomplete'):
            undo_action(store,action,{account:client},now)
    assert attempts==['COPY']
    step = store.conn.execute("SELECT * FROM action_steps WHERE operation='delete_copy'").fetchone()
    assert step['phase']=='deleting' and step['outcome']=='unknown' and step['backup_status']=='verified'


def test_spotify_receipt_revision_and_destination_fence_stale_workers(store):
    from yt_playlist.core import workflow_settings
    account = store.accounts.upsert_account('Owner',None,True)
    original = {'creation_phase':'prepared','marker':'marker','complete':False}
    store.settings.save_spotify_transfer(account,'fingerprint',original)
    first = store.settings.spotify_transfer_receipts(account)['fingerprint']
    stale = dict(first)
    first.update(creation_phase='created',ytm_playlist_id='DESTINATION',source_video_ids=['one',None,'one'])
    store.settings.save_spotify_transfer(account,'fingerprint',first)
    with pytest.raises(ValueError,match='stale revision'):
        store.settings.save_spotify_transfer(account,'fingerprint',stale)
    latest = store.settings.spotify_transfer_receipts(account)['fingerprint']
    latest['ytm_playlist_id'] = 'OTHER'
    with pytest.raises(ValueError,match='marker or destination'):
        store.settings.save_spotify_transfer(account,'fingerprint',latest)
    assert workflow_settings.receipts(store.conn,'playlist',account)['fingerprint']['source_video_ids']==['one',None,'one']


def test_tv_append_intent_preserves_order_and_completion_across_reload(store):
    checkpoint = {'request_id':'request','state':'pending','at':3,'before':['one','one'],
                  'items':[{'video_id':'two','title':'Two'}],'additions':[{'video_id':'two','title':'Two'}]}
    recipe = {'tv_stream':'stream','tv_refill':checkpoint}
    store.playlist_recipes.set_recipe('TV',recipe,3)
    assert store.playlist_recipes.get_recipe('TV')==recipe
    assert json.loads(store.conn.execute("SELECT tv_appends FROM playlist_scopes WHERE id='TV'").fetchone()[0])[0][
        'checkpoint']['before'] == ['one', 'one']
    complete = {**recipe,'tv_refill':{'request_id':'request','state':'complete','items':checkpoint['items']}}
    store.playlist_recipes.set_recipe('TV',complete,4)
    assert store.playlist_recipes.get_recipe('TV')==complete
    with pytest.raises(ValueError,match='completed TV append'):
        store.playlist_recipes.set_recipe('TV',recipe,5)
    assert store.playlist_recipes.get_recipe('TV')==complete


def test_radio_pending_and_replacement_have_typed_receipts_and_revision_fence(store):
    from yt_playlist.core import radio_storage
    account = store.accounts.upsert_account('Owner',None,True)
    state = {'version':2,'episode_id':'episode','identity':account,'phase':'appending','playlist':'RADIO',
             'tracks':[{'video_id':'one'}],'pending':{'video_id':'two'},'pending_sent':True,
             'pending_started_at':3,'pending_result':{'status':'STATUS_SUCCEEDED','videoId':'two','setVideoId':'handle'},'position':0}
    with store.conn:
        token = radio_storage.save(store.conn,state,expected=None,now=3)
    assert radio_storage.load(store.conn)==(state,token)
    replacement = {'allowed':[[],['one','one']],'confirmed':['one','one'],'waiting':['one','two'],
        'waiting_at':4,'receipt':None,'adding':{'video_id':'two'},'proposed':[{'video_id':'two','title':'Two'}],
        'play_prefix':[],'slot':0,'choice_id':'choice','excluded':['three']}
    newer = {**state,'phase':'replacing','pending':None,'pending_sent':False,'pending_result':None,
             'pending_started_at':None,'replacement':replacement}
    with store.conn:
        current_token = radio_storage.save(store.conn,newer,expected=token,now=4)
    assert radio_storage.load(store.conn)==(newer,current_token)
    with pytest.raises(ValueError,match='changed'),store.conn:
        radio_storage.save(store.conn,state,expected=token,now=5)
    with pytest.raises(sqlite3.IntegrityError,match='revision'),store.conn:
        store.conn.execute("UPDATE radio_sessions SET operation_json=json_set(operation_json,'$.session_revision',1)")


def test_terminal_action_and_frozen_review_are_guarded_in_sql(store):
    action = store.actions.record_action('add_tracks','{}','{}','pending','{}',1)
    store.actions.update_action(action,'cancelled',2)
    with pytest.raises(sqlite3.IntegrityError,match='transition'),store.conn:
        store.conn.execute("UPDATE actions SET status='executed' WHERE id=?",(action,))
    with pytest.raises(sqlite3.IntegrityError,match='identity'),store.conn:
        store.conn.execute("UPDATE actions SET kind='delete_playlist' WHERE id=?",(action,))


def test_cloned_recovery_relocates_backup_references_without_original_directory(
        tmp_path, monkeypatch, _pristine_current_schema):
    from pathlib import Path
    from tests.library_image.capture import rebase
    from tests.library_image.file_references import action_backup_references
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core import action_backup_files, fao
    from yt_playlist.core.store import Store
    from yt_playlist.library.playlist_undo import undo_action
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    original = Store(tmp_path/'original.db')
    restored = None
    try:
        initialize_consumer(original, _pristine_current_schema)
        client = RecoveryClient({'SOURCE':['one','one','two']})
        account,action = _deleted(original,client)
        prior = original.actions.claim_undo(action,3)
        from yt_playlist.library.playlist_undo import _steps
        undo = json.loads(prior.undo_json)
        undo['recovery'] = {'version':2,'steps':_steps(undo,action)}
        original.actions.save_undo_progress(action,undo,3,claim_token=prior.execution_token)
        original.conn.execute('BEGIN')
        with closing(sqlite3.connect(tmp_path/'restored.db')) as copied, copied:
            original.conn.backup(copied)
        collection_manifest = collection_recovery.export(original.conn,tmp_path/'collections')
        backup_manifest = action_backup_files.export(original.conn,tmp_path/'backups')
        original.conn.rollback()
        retained_ids = {row['position']: action_backup_files.record_id(row['retained_filename'])
                        for row in action_backup_files.inventory(original.conn)}
        retained_ages = {row['position']: fao.read(row['retained_filename']).info.ctime_ns
                         for row in action_backup_files.inventory(original.conn)}
        assert all(retained_ids.values())
        for row in action_backup_files.inventory(original.conn):
            Path(row['filename']).unlink(missing_ok=True)
            Path(row['retained_filename']).unlink(missing_ok=True)
        restored = Store(tmp_path/'restored.db')
        original_refs = action_backup_files.inventory(original.conn)
        captured = rebase(restored.conn, tmp_path/'original.db', tmp_path/'restored.db')
        assert {Path(item['retained_filename']) for item in original_refs} <= captured
        assert {path for _, _, path in action_backup_references(restored.conn)} == {
            action_backup_files.record_path(collection_recovery.directory(restored.conn), identity)
            for identity in retained_ids.values()}
        assert [item['filename'] for item in action_backup_files.inventory(restored.conn)] == [
            item['filename'] for item in original_refs]
        collection_recovery.restore(restored.conn,collection_manifest)
        action_backup_files.restore(restored.conn,backup_manifest)
        for row in action_backup_files.inventory(restored.conn):
            assert action_backup_files.record_id(row['retained_filename']) == retained_ids[row['position']]
            assert fao.read(row['retained_filename']).info.ctime_ns == retained_ages[row['position']]
            assert Path(row['retained_filename']).is_relative_to(collection_recovery.directory(restored.conn))
        restored._bind_repositories()
        undo_action(restored,action,{account:client},4)
        assert restored.actions.get_action(action).status=='undone'
        assert [row['videoId'] for row in client._tracks['PL_NEW_0']]==['one','one','two']
    finally:
        original.conn.close()
        if restored:
            restored.conn.close()
