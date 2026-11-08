"""Collection cutover, independent edits and restart through actual owner APIs."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core import account_auth_state, album_feedback_storage, collection_recovery, genre_identity, model_state, radio_storage, sync_state
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store
from yt_playlist.library import sync_outcomes
from yt_playlist.rec import album_feedback
from yt_playlist.rec import album_tuning


def installed_92(path):
    store = Store(path)
    try:
        for sql in current.statements(version=92):
            store.conn.execute(sql)
        genre_identity.seed_catalog(store.conn)
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
        store.conn.execute('INSERT INTO rec_preparation VALUES (1,0,0)')
        store.conn.execute('INSERT INTO rec_group_clock VALUES (1,0)')
        store.conn.execute('INSERT INTO group_retention_state VALUES (1,0)')
        store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                               [(item.version,item.name) for item in registry.MIGRATIONS[:92]])
        store.conn.commit()
        store._bind_repositories()
        assert [row[1] for row in store.conn.execute('PRAGMA table_info(road_trip_drafts)')]==['recipe_id','state','updated_at']
        return store
    except BaseException:
        store.conn.close()
        raise


def test_independent_feedback_and_withdrawal_keep_lineage(tmp_path):
    path = tmp_path/'feedback.db'
    store = Store(path)
    store.init_schema()
    store.accounts.upsert_account('Main','main.json',None,True)
    def respond(browse):
        other = Store(path)
        other._bind_repositories()
        try:
            album_feedback.set_feedback(other,browse,'more',100)
        finally:
            other.conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(respond,['one','two']))
    assert store.conn.execute('SELECT count(*) FROM album_feedback WHERE withdrawn_at IS NULL').fetchone()[0]==2
    album_feedback.set_feedback(store,'one','clear',101)
    assert store.conn.execute('SELECT count(*) FROM album_feedback WHERE withdrawn_at IS NULL').fetchone()[0]==1
    assert store.conn.execute('SELECT count(*) FROM album_feedback WHERE withdrawn_at=101').fetchone()[0]==1
    assert store.conn.execute('SELECT count(*) FROM album_feedback').fetchone()[0]==2


def test_scoped_feedback_does_not_withdraw_another_accounts_legacy_fallback(store):
    first = store.accounts.upsert_account('First','first.json',None,True)
    second = store.accounts.upsert_account('Second','second.json',None,False)
    with store.conn:
        album_feedback_storage.import_legacy(store.conn,{'release':{'kind':'more','at':1}})
        album_feedback_storage.replace(store.conn,'release','reject',2,account_id=first)
    assert [row['response'] for row in album_feedback_storage.rows(store.conn,account_id=first)]==['reject']
    assert [row['response'] for row in album_feedback_storage.rows(store.conn,account_id=second)]==['more']
    with store.conn:
        album_feedback_storage.replace(store.conn,'release','clear',3,account_id=first)
    assert album_feedback_storage.rows(store.conn,account_id=first)==[]
    assert [row['response'] for row in album_feedback_storage.rows(store.conn,account_id=second)]==['more']
    assert tuple(store.conn.execute('SELECT account_id,withdrawn_at FROM album_feedback WHERE account_id IS NULL').fetchone())==(None,None)


def test_draft_revision_fences_other_connection_and_requires_account(tmp_path):
    path = tmp_path/'draft.db'
    store = Store(path)
    store.init_schema()
    recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],[],60,1)
    with pytest.raises(ValueError,match='account'):
        store.road_trip.save_road_trip_draft(recipe,{'revision':'first'},2)
    store.accounts.upsert_account('Main','main.json',None,True)
    store.road_trip.save_road_trip_draft(recipe,{'revision':'first','picks':['one'],'banned':[]},3)
    other = Store(path)
    other._bind_repositories()
    try:
        store.road_trip.save_road_trip_draft(recipe,{'revision':'edited','picks':['two'],'banned':[]},4)
        assert not other.road_trip.replace_road_trip_draft(recipe,{'revision':'stale','picks':['one']},5,expected_revision='first')
        assert other.road_trip.get_road_trip_draft(recipe)['picks']==['two']
    finally:
        other.conn.close()


def test_radio_stale_worker_cannot_overwrite_stop_or_replay_pending_write(store):
    account = store.accounts.upsert_account('Main','main.json',None,True)
    state = {'version':2,'episode_id':'episode','identity':account,'phase':'appending','playlist':'RADIO',
             'tracks':[{'video_id':'one','title':'One','artist':'Artist'}],'pending':{'video_id':'two'},
             'pending_sent':True,'pending_started_at':10,'position':0}
    with store.conn:
        revision = radio_storage.save(store.conn,state,expected=None,now=10)
    loaded,token = radio_storage.load(store.conn)
    assert loaded==state and token==revision
    assert loaded['pending_sent'] is True
    with store.conn:
        newer = radio_storage.save(store.conn,{**state,'phase':'stopped','stopped':True},expected=token,now=11)
    with pytest.raises(ValueError,match='changed'),store.conn:
        radio_storage.save(store.conn,state,expected=token,now=12)
    assert radio_storage.load(store.conn)[1]==newer
    assert radio_storage.load(store.conn)[0]['phase']=='stopped'
    assert store.conn.execute('SELECT state FROM radio_queue_items ORDER BY position DESC LIMIT 1').fetchone()[0]=='pending'


def test_sync_interruption_and_superseded_completion(store):
    account = store.accounts.upsert_account('Main','main.json',None,True)
    first = sync_outcomes.begin_attempt(store,100)
    result = {'status':'failed','accounts':{str(account):{'status':'failed','resources':{
              'history':{'status':'failed','message':'offline'}}}},'resources':{}}
    sync_outcomes.finish_attempt(store,result,100,expected_revision=first)
    assert sync_outcomes.SyncSchedule(store,101,automatic=True).previous(account,'history')['retry_at']==400
    second = sync_outcomes.begin_attempt(store,200)
    assert sync_state.read(store.conn)['accounts'][str(account)]['resources']['history']['status']=='failed'
    third = sync_outcomes.begin_attempt(store,201)
    with pytest.raises(ValueError,match='superseded'):
        sync_outcomes.finish_attempt(store,result,202,expected_revision=second)
    assert store.conn.execute('SELECT revision,phase FROM sync_attempts').fetchone()[:]==(third,'running')


def test_collection_backup_exact_coverage_and_reader_fenced_cleanup(store,tmp_path):
    store.accounts.upsert_account('Main','main.json',None,True)
    recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],[],60,1)
    state = {'revision':'held','picks':['one'],'banned':[],'seed':7}
    store.road_trip.save_road_trip_draft(recipe,state,2)
    with store.conn:
        model_state.put(store.conn,{'rec_content_model':'{"ncat":0,"cont":[]}'})
    store.conn.execute('BEGIN')
    manifest = collection_recovery.export(store.conn,tmp_path/'export')
    store.conn.rollback()
    collection_recovery.restore(store.conn,manifest)
    assert store.road_trip.get_road_trip_draft(recipe)==state
    filenames = [Path(row[3]) for row in collection_recovery.references(store.conn)]
    with store.conn:
        store.road_trip.delete_road_trip_draft(recipe)
    from yt_playlist.core import policy_documents
    with policy_documents.retained_reader(collection_recovery.directory(store.conn)):
        assert collection_recovery.collect_orphans(store.conn)==0
        assert all(path.is_file() for path in filenames)
    assert collection_recovery.collect_orphans(store.conn)>=1
    assert store.settings.get_setting('rec_content_model')=='{"ncat":0,"cont":[]}'
    with pytest.raises(ValueError,match='owners'):
        collection_recovery.restore(store.conn,manifest)


def test_temporary_genre_choices_keep_identity_after_alias_edit_and_merge(store):
    store.genres.configure_catalog()
    store.genres.set_genre_alias('Saved menu choice','synth pop')
    album_tuning.apply(store,100,{'novelty':.6},['Saved menu choice','An unknown historical style'])
    store.genres.set_genre_alias('Saved menu choice','drum bass')
    assert album_tuning.state(store,101)['genres']==['An unknown historical style','synth pop']
    store.genres.rename_genre('synth pop','A new display name')
    assert album_tuning.state(store,102)['genres']==['An unknown historical style','synth pop']
    store.genres.merge_genres('synth pop','drum bass')
    assert album_tuning.state(store,103)['genres']==['An unknown historical style','drum bass']
    assert album_tuning.state(store,100+album_tuning.DURATION_S)['remaining']==0
    album_tuning.reset(store)
    assert not store.conn.execute('SELECT 1 FROM album_menu_control_genres').fetchone()


def test_account_auth_updates_and_clears_do_not_replace_other_context(store):
    from yt_playlist.web.context import Ctx
    first = store.accounts.upsert_account('First','first.json',None,True)
    second = store.accounts.upsert_account('Second','second.json',None,False)
    def context():
        return Ctx(store=store,client_provider=lambda:{},now_fn=lambda:1,templates=None,jobs=None)
    earlier,later = context(),context()
    earlier.flag_auth_expired(first,'First')
    later.flag_auth_expired(second,'Second')
    assert context().auth_expired=={first:'First',second:'Second'}
    earlier.clear_auth_expired(first)
    assert context().auth_expired=={second:'Second'}
    with pytest.raises(ValueError,match='recorded account'),store.conn:
        account_auth_state.flag(store.conn,999,'Unknown')
    assert not store.conn.execute("SELECT 1 FROM settings WHERE key='auth_expired_identities'").fetchone()


def test_failed_collection_conversion_rolls_back_schema_and_retains_original(tmp_path, legacy_owner_store):
    old = legacy_owner_store(tmp_path/'invalid-collection.db')
    old.settings.set_setting('album_feedback','{"release":{"kind":"more","at":1,"unknown":true}}')
    from yt_playlist.core.migrations import MigrationError
    with pytest.raises(MigrationError,match='original retained'):
        migrate(old.conn, registry=registry.MIGRATIONS)
    assert old.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==93
    assert old.settings.get_setting('album_feedback')=='{"release":{"kind":"more","at":1,"unknown":true}}'
    assert not old.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_feedback'").fetchone()
    assert [row[1] for row in old.conn.execute('PRAGMA table_info(road_trip_drafts)')]==['recipe_id','state','updated_at']


def test_collection_sql_rejects_cross_account_scope_and_wrong_recording(store):
    from yt_playlist.core import album_playback_context, playlist_scopes
    first = store.accounts.upsert_account('First','first.json',None,True)
    second = store.accounts.upsert_account('Second','second.json',None,False)
    entry = store.tracks.upsert_track('first-video','First song','Artist',None,100)
    other = store.tracks.upsert_track('other-video','Other song','Artist',None,100)
    recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],[],60,1)
    store.road_trip.save_road_trip_draft(recipe,{'revision':'held','picks':['first-video']},2)
    wrong_recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',(other,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError,match='another recording'),store.conn:
        store.conn.execute('UPDATE road_trip_draft_entries SET recording_id=?',(wrong_recording,))
    with store.conn:
        album_playback_context.put(store.conn,{'opportunity_id':'choice','browse':'release','playlist':'LM',
                                              'identity_id':first,'at':3,'videos':['first-video','first-video']})
        wrong_scope = playlist_scopes.ensure(store.conn,'LM',second)
    with pytest.raises(sqlite3.IntegrityError,match='another account'),store.conn:
        store.conn.execute('UPDATE album_playback_intents SET playlist_scope_id=?',(wrong_scope,))
    assert album_playback_context.load(store.conn)['videos']==['first-video','first-video']
    with pytest.raises(sqlite3.IntegrityError,match='domain owner'),store.conn:
        store.conn.execute("INSERT INTO settings VALUES ('radio_current_session','{}')")
