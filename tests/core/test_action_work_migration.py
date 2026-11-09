"""Explicit action ownership survives supported legacy formats and lost workers."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import native_sql_calls, numbered_store, without_query_timing
from yt_playlist.core import account_auth_state, action_work, collection_recovery, playlist_scopes
from yt_playlist.core import entity_names, schema_inspection, workflow_settings
from yt_playlist.core.migrations import current, migrate, registry


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Action ownership checks drive local collaborators without the HTTP app."""


def legacy_account(store,label='Original'):
    # Installed-v92 fixtures seed their physical predecessor vocabulary. Current
    # repositories use the canonical account names after the real upgrade.
    with store.conn:
        return store.conn.execute('INSERT INTO identities(label,credential_ref,is_master) VALUES (?,?,1)',
                                  (label,'original.json')).lastrowid


def legacy_playlist(store,account,remote):
    with store.conn:
        scope = playlist_scopes.ensure(store.conn,remote,account)
        return store.conn.execute('INSERT INTO main.playlists(identity_id,ytm_playlist_id,scope_ref,title,track_count) '
                                  'VALUES (?,?,?,?,0)',(account,remote,scope,remote)).lastrowid


def backup(tmp_path,account,remote):
    path = tmp_path/(remote+'.json')
    path.write_text(json.dumps({'snapshot_version':2,'identity_id':account,'ytm_playlist_id':remote,
                                'title':remote,'tracks':[{'video_id':'one'},{'video_id':'one'}]}))
    return str(path)


def seed_legacy_action_workflows(store):
    account = legacy_account(store, 'Main')
    membership = legacy_playlist(store, account, 'NEWER')
    identity = store.actions.record_action('plan', json.dumps({
        'source': membership, 'mode': 'delete', 'source_ytm': 'ORIGINAL'}),
        '{}', 'executed', '{}', 2)
    receipt = {'creation_phase': 'creating', 'marker': 'original marker', 'complete': False, 'updated_at': 3,
               'last_run_added': 0, 'source_video_ids': ['one', None, 'one']}
    key = workflow_settings.PLAYLIST + ':' + str(account)
    legacy_key = workflow_settings.PLAYLIST
    gc_key = workflow_settings.GC + '[999,"MISSING"]'
    gc = {'identity': 999, 'ytm': 'MISSING', 'attempts': 4, 'last_attempt_at': 3,
          'next_retry_at': 3603, 'phase': 'reconcile'}
    store.settings.set_setting(key, json.dumps({'source-fingerprint': receipt}))
    store.settings.set_setting(legacy_key, json.dumps(['old-unattributed-fingerprint']))
    store.settings.set_setting(gc_key, json.dumps(gc))
    store.settings.set_setting(account_auth_state.KEY, json.dumps({str(account): 'Main', '999': 'Retired'}))
    return {'account': account, 'membership': membership, 'action': identity, 'receipt': receipt,
            'key': key, 'legacy_key': legacy_key, 'gc_key': gc_key, 'gc': gc}


@pytest.fixture(scope='module')
def action_upgrade_inputs(upgraded_custody_inputs):
    def get(target, **options):
        return upgraded_custody_inputs(92, target, 'legacy-action-workflows', seed_legacy_action_workflows, **options)

    get.release = lambda: upgraded_custody_inputs.release(92, 'legacy-action-workflows')
    return get


def test_action_upgrade_schema_input_keeps_installed_history_and_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(92), 92)


def test_action_upgrade_input_keeps_ordered_receipts_and_original_remote(action_upgrade_inputs):
    template = action_upgrade_inputs(92)
    assert_upgrade_phase(template, 92)
    values = template.result
    conn = template.source.conn
    assert json.loads(conn.execute('SELECT value FROM settings WHERE key=?', (values['key'],)).fetchone()[0]) == {
        'source-fingerprint': values['receipt']}
    assert json.loads(conn.execute('SELECT params_json FROM actions WHERE id=?', (values['action'],)).fetchone()[0]) == {
        'source': values['membership'], 'mode': 'delete', 'source_ytm': 'ORIGINAL'}
    assert conn.execute('SELECT ytm_playlist_id FROM main.playlists WHERE id=?', (values['membership'],)).fetchone()[0] == 'NEWER'


def assert_original_action_target(conn, values):
    row = conn.execute('SELECT * FROM action_targets WHERE action_id=?', (values['action'],)).fetchone()
    assert row['account_id'] is None and row['membership_ref_id'] is None
    assert row['unresolved_membership_ref_id'] == values['membership']
    assert row['provider_playlist_id_snapshot'] == 'ORIGINAL'
    assert row['resolution_reason'] == 'membership_snapshot_mismatch'


@pytest.mark.parametrize('version,target,phase', custody_upgrade_cases((92,)))
def test_action_upgrade_preserves_real_history_and_unresolved_target(action_upgrade_inputs, version, target, phase):
    template = action_upgrade_inputs(target, phase=phase)
    installed = target if phase == 'converted' else custody_predecessor(version, target)
    assert_upgrade_phase(template, installed)
    if installed >= 98:
        assert_original_action_target(template.source.conn, template.result)
    if installed >= 94:
        assert account_auth_state.read(template.source.conn) == {template.result['account']: 'Main', 999: 'Retired'}


def test_action_upgrade_serving_input_keeps_real_revision_guards(action_upgrade_inputs):
    template = action_upgrade_inputs(107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn
    assert_original_action_target(template.source.conn, template.result)


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
@pytest.mark.parametrize('numbered_store', [92], indirect=True)
def test_legacy_auth_unknown_account_is_retained_without_assigning_current_master(numbered_store):
    store, _ = numbered_store
    master = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.settings.set_setting(account_auth_state.KEY, json.dumps({str(master): 'Main', '999': 'Retired'}))
    # Authentication owns this populated conversion, not the action/receipt
    # template used by the other workflows. Keep the complete original upgrade.
    conn = store.conn
    entity_names.clear_predecessor_reads(conn)
    migrate(conn, registry=registry.MIGRATIONS)
    assert account_auth_state.read(conn) == {master: 'Main', 999: 'Retired'}
    assert tuple(conn.execute('SELECT account_id,unresolved_account_id FROM account_auth_expirations '
                              'WHERE unresolved_account_id=999').fetchone()) == (None, 999)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def upgraded_action_store(action_upgrade_inputs, tmp_path):
    template = action_upgrade_inputs(107)
    with template.open(tmp_path / 'actions.db') as store:
        with schema_inspection.reuse_tables(store.conn):
            yield store, template.result


def test_expired_claim_requires_reconciliation_and_fences_stale_completion(store,tmp_path):
    account = store.accounts.upsert_account('Owner','owner.json',None,True)
    store.playlists.upsert_playlist(account,'SOURCE','Source',2,'',1)
    undo = {'deletions':[{'identity':account,'ytm':'SOURCE','phase':'pending','backup':backup(tmp_path,account,'SOURCE')}]}
    action = store.actions.record_action('delete_playlist','{}','{}','delete_pending',json.dumps(undo),2)
    target = store.conn.execute('SELECT id FROM action_targets WHERE action_id=?',(action,)).fetchone()[0]
    with store.conn:
        first = action_work.claim_target(store.conn,target,3,lease_seconds=5)
        store.conn.execute("UPDATE action_steps SET phase='uncertain',outcome='unknown' WHERE target_id=?",(target,))
    with pytest.raises(ValueError,match='Another action worker'):
        with store.conn:
            action_work.claim_target(store.conn,target,4,reconcile=True)
    with pytest.raises(ValueError,match='outcome is unknown'):
        with store.conn:
            action_work.claim_target(store.conn,target,9)
    with store.conn:
        replacement = action_work.claim_target(store.conn,target,9,reconcile=True)
    with pytest.raises(ValueError,match='stale worker'):
        action_work.require_claim(store.conn,target,first)
    action_work.require_claim(store.conn,target,replacement)


def test_idempotent_intent_and_target_agreement_are_enforced(store):
    account = store.accounts.upsert_account('Owner','owner.json',None,True)
    other = store.accounts.upsert_account('Other','other.json',None,False)
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
    account = store.accounts.upsert_account('Owner','owner.json',None,True)
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
        step = store.conn.execute("SELECT s.*,a.reviewed_draft_revision FROM action_steps s JOIN actions a ON a.id=s.action_id "
            "JOIN action_targets t ON t.id=s.target_id WHERE s.operation='merge_plan' AND t.provider_playlist_id_snapshot=?",
            (remote,)).fetchone()
        assert step['phase']=='uncertain' and step['outcome']=='unknown'
        assert step['reviewed_draft_revision']==draft.revision
        assert step['claim_token']
        action_work.backup_reference(step['backup_filename'])
        return remove(remote,tracks)
    monkeypatch.setattr(client,'remove_playlist_items',guarded_remove)
    apply_result(store,{account:client},memberships,['one'],'all',2,reviewed_draft=(draft_id,draft.revision))
    row = store.conn.execute("SELECT * FROM actions WHERE kind='apply_merge'").fetchone()
    captured = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='action',owner=row['id'])
    assert captured['reviewed_draft']['params']=='{"keep":"all","edited":true}'
    store.actions.delete_action(draft_id)
    assert store.actions.get_action(row['id']) is not None
    assert store.conn.execute('SELECT count(*) FROM action_target_claims').fetchone()[0]==0


def test_export_retains_backup_bytes_after_original_file_is_removed(store,tmp_path,monkeypatch):
    from pathlib import Path
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from yt_playlist.core import action_backup_files, fao
    from yt_playlist.library.playlist_undo import undo_action
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path/'home'))
    client = RecoveryClient({'SOURCE':['one','one','two']})
    account,action = _deleted(store,client)
    original = store.conn.execute('SELECT filename,retained_filename FROM action_backups WHERE action_id=?',(action,)).fetchone()
    assert fao.read(original[0]).bytes==fao.read(original[1]).bytes
    store.conn.execute('BEGIN')
    manifest = action_backup_files.export(store.conn,tmp_path/'export')
    store.conn.rollback()
    Path(original[0]).unlink()
    Path(original[1]).unlink()
    action_backup_files.restore(store.conn,manifest)
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


def test_workflow_settings_upgrade_keeps_unknown_accounts_and_ordered_transfer_slots(upgraded_action_store):
    store, values = upgraded_action_store
    assert json.loads(store.settings.get_setting(values['key'])) == {'source-fingerprint': values['receipt']}
    assert json.loads(store.settings.get_setting(values['legacy_key'])) == ['old-unattributed-fingerprint']
    assert json.loads(store.settings.get_setting(values['gc_key'])) == values['gc']
    assert store.conn.execute('SELECT account_id FROM playlist_gc_attempts').fetchone()[0] is None
    assert store.conn.execute("SELECT account_id FROM spotify_transfer_receipts WHERE fingerprint='old-unattributed-fingerprint'").fetchone()[0] is None
    assert not any(workflow_settings.owns(row[0]) for row in store.conn.execute('SELECT key FROM settings'))
    current.validate(store.conn)


def test_spotify_receipt_revision_and_destination_fence_stale_workers(store):
    from yt_playlist.core import workflow_settings
    account = store.accounts.upsert_account('Owner','owner.json',None,True)
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


def test_reused_historical_membership_keeps_original_remote_without_new_account(upgraded_action_store):
    store, values = upgraded_action_store
    identity = values['action']
    assert_original_action_target(store.conn, values)
    store.actions.update_action(identity,'executed',3)
    assert json.loads(store.actions.get_action(identity).params_json)['source_ytm']=='ORIGINAL'


def test_tv_append_intent_preserves_order_and_completion_across_reload(store):
    checkpoint = {'request_id':'request','state':'pending','at':3,'before':['one','one'],
                  'items':[{'video_id':'two','title':'Two'}],'additions':[{'video_id':'two','title':'Two'}]}
    recipe = {'tv_stream':'stream','tv_refill':checkpoint}
    store.playlist_recipes.set_recipe('TV',recipe,3)
    assert store.playlist_recipes.get_recipe('TV')==recipe
    assert [row[0] for row in store.conn.execute("SELECT video_id_snapshot FROM tv_append_items WHERE role='before' ORDER BY position")]==['one','one']
    complete = {**recipe,'tv_refill':{'request_id':'request','state':'complete','items':checkpoint['items']}}
    store.playlist_recipes.set_recipe('TV',complete,4)
    assert store.playlist_recipes.get_recipe('TV')==complete
    with pytest.raises(ValueError,match='completed TV append'):
        store.playlist_recipes.set_recipe('TV',recipe,5)
    assert store.playlist_recipes.get_recipe('TV')==complete


def test_radio_pending_and_replacement_have_typed_receipts_and_revision_fence(store):
    from yt_playlist.core import radio_storage
    account = store.accounts.upsert_account('Owner','owner.json',None,True)
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
        store.conn.execute('UPDATE radio_operations SET session_revision=1')


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
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core import action_backup_files
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
        for row in action_backup_files.inventory(original.conn):
            Path(row['filename']).unlink(missing_ok=True)
            Path(row['retained_filename']).unlink(missing_ok=True)
        restored = Store(tmp_path/'restored.db')
        collection_recovery.restore(restored.conn,collection_manifest)
        action_backup_files.restore(restored.conn,backup_manifest)
        restored._bind_repositories()
        undo_action(restored,action,{account:client},4)
        assert restored.actions.get_action(action).status=='undone'
        assert [row['videoId'] for row in client._tracks['PL_NEW_0']]==['one','one','two']
    finally:
        original.conn.close()
        if restored:
            restored.conn.close()


def test_action_upgrade_inputs_release_their_private_files_and_sql(action_upgrade_inputs):
    sources = action_upgrade_inputs.release()
    assert len(sources) == len(custody_upgrade_cases((92,))) // 2 + 1
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
