"""Import job admission and saved document recovery through a real upgrade."""
import json
import sqlite3

import pytest

from tests.core.test_collection_state_migration import installed_92
from yt_playlist.core import collection_recovery, spotify_job_state
from yt_playlist.core.migrations import current, migrate, registry, MigrationError
from yt_playlist.core.store import Store
from yt_playlist.web.spotify_import import load_job, save_job


def test_upgrade_retains_import_unknown_account_and_verbatim_canvas(tmp_path):
    path = tmp_path/'documents.db'
    store = installed_92(path)
    job = {'status':'running','identity_id':999,'identity_label':'Retired account','started_at':2,
           'report':{'partial':[1,2]},'dismissed':None}
    store.settings.set_setting(spotify_job_state.KEY,json.dumps(job))
    recipe = {'media':'audio','model':'fresh','facets':{'genres':['house','Unknown original','house'],'eras':['2010']},
              'opaque':{'positions':[2,2,1]}}
    store.playlist_recipes.set_recipe('PLAYLIST',recipe,3)
    canvas = 'legacy canvas text: {\n  no JSON requirement\n}'
    store.playlist_recipes.set_cluster_canvas('PLAYLIST',canvas,4)
    migrate(store.conn, registry=registry.MIGRATIONS)
    store._bind_repositories()
    assert json.loads(store.settings.get_setting(spotify_job_state.KEY))==job
    assert store.playlist_recipes.get_recipe('PLAYLIST')==recipe
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST')==canvas
    assert store.conn.execute('SELECT account_id,unresolved_account_id FROM spotify_import_jobs').fetchone()[:]==(None,999)
    assert store.conn.execute('SELECT count(*) FROM playlist_recipe_facets').fetchone()[0]==4
    store.conn.close()
    restarted = Store(path)
    try:
        restarted.init_schema()
        assert restarted.playlist_recipes.get_cluster_canvas('PLAYLIST')==canvas
        interrupted = load_job(restarted)
        interrupted['status']='error'
        save_job(restarted,interrupted)
        assert load_job(restarted)['account_id']==999
        current.validate(restarted.conn)
    finally:
        restarted.conn.close()


def test_stale_import_completion_cannot_replace_report_or_change_account(store):
    first = store.accounts.upsert_account('First','first.json',None,True)
    second = store.accounts.upsert_account('Second','second.json',None,False)
    job = {'status':'running','identity_id':first}
    save_job(store,job)
    stale = dict(job)
    with pytest.raises(ValueError,match='account cannot change'):
        save_job(store,{**job,'identity_id':second})
    save_job(store,{**job,'status':'done','report':{'complete':True}})
    with pytest.raises(ValueError,match='replaced or updated'):
        save_job(store,{**stale,'status':'error'})
    store.settings.delete_setting(spotify_job_state.KEY)
    with pytest.raises(ValueError,match='replaced or updated'):
        save_job(store,stale)
    assert load_job(store) is None
    with pytest.raises(ValueError,match='requires its account'):
        save_job(store,{'status':'running','identity_id':999})


def test_current_recipe_facets_follow_merge_while_capture_and_backup_remain(store,tmp_path):
    recipe = {'facets':{'genres':['synth pop','Unknown captured','synth pop'],'eras':['1990']}}
    store.playlist_recipes.set_recipe('PLAYLIST',recipe,3)
    before = store.conn.execute('SELECT * FROM rec_recipes').fetchone()
    store.genres.set_genre_alias('Unknown captured','drum bass')
    store.genres.merge_genres('synth pop','drum bass',now=4)
    assert store.playlist_recipes.get_recipe('PLAYLIST')['facets']['genres']==['drum bass','Unknown captured','drum bass']
    assert collection_recovery.read(before['recovery_filename'],before['recovery_digest'],before['recovery_bytes'],
                                    kind='playlist-recipe',owner=before['id'])==recipe
    store.playlist_recipes.set_cluster_canvas('PLAYLIST','canvas',5)
    store.conn.execute('BEGIN')
    manifest = collection_recovery.export(store.conn,tmp_path/'backup')
    store.conn.rollback()
    collection_recovery.restore(store.conn,manifest)
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST')=='canvas'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    axis = store.conn.execute("SELECT id FROM taste_axes WHERE kind='genre' LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError,match='axis disagrees'):
        store.conn.execute("UPDATE playlist_recipe_facets SET axis_id=? WHERE kind='era'",(axis,))
    store.conn.rollback()


def test_explicit_null_recipe_fields_and_empty_canvas_remain_distinct(store):
    store.playlist_recipes.set_recipe('EMPTY',{'media':None,'facets':None},None)
    store.playlist_recipes.set_cluster_canvas('EMPTY','',None)
    assert store.playlist_recipes.get_recipe('EMPTY')=={'media':None,'facets':None}
    assert store.playlist_recipes.get_cluster_canvas('EMPTY')==''
    assert store.playlist_recipes.get_recipe_created_ats()=={'EMPTY':None}


def test_recording_alias_rekeys_saved_documents_and_preserves_unknown_formats(store):
    from yt_playlist.core import catalog
    first = store.tracks.upsert_track('one','One','Artist',None,100)
    second = store.tracks.upsert_track('two','Two','Artist',None,100)
    old = store.tracks.catalog_subject(first)['identity_key']
    second_recording = store.tracks.catalog_subject(second)['recording_id']
    store.playlist_recipes.set_recipe('PLAYLIST',{'keys':[old],'label':old},3)
    store.playlist_recipes.set_cluster_canvas('PLAYLIST',json.dumps({'nodes':[{'key':old}],'label':old}),4)
    future = {'catalog_state_version':999,'keys':[old]}
    store.playlist_recipes.set_recipe('FUTURE',future,5)
    store.playlist_recipes.set_cluster_canvas('FUTURE','opaque text',6)
    with store.conn:
        catalog.add_alias(store.conn,old,second_recording,'test')
    current_key = store.tracks.catalog_subject(first)['identity_key']
    assert current_key!=old
    recipe = store.playlist_recipes.get_recipe('PLAYLIST')
    canvas = json.loads(store.playlist_recipes.get_cluster_canvas('PLAYLIST'))
    assert recipe['keys']==[current_key] and recipe['label']==old
    assert canvas['nodes']==[{'key':current_key,'catalog_identity_version':2}]
    assert canvas['label']==old
    assert store.playlist_recipes.get_recipe('FUTURE')==future
    assert store.playlist_recipes.get_cluster_canvas('FUTURE')=='opaque text'


def test_unsupported_job_rolls_back_original_documents(tmp_path):
    store = installed_92(tmp_path/'rollback.db')
    raw = json.dumps({'status':'running','unclassified_new_field':True})
    store.settings.set_setting(spotify_job_state.KEY,raw)
    store.playlist_recipes.set_cluster_canvas('PLAYLIST','untouched',2)
    with pytest.raises(MigrationError,match='Unsupported Spotify import job'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert store.conn.execute("SELECT value FROM settings WHERE key=?",(spotify_job_state.KEY,)).fetchone()[0]==raw
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST')=='untouched'
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==96
