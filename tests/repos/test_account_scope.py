import json

import pytest

from yt_playlist.core.accounts import client_for


def test_system_curation_is_per_account_and_survives_membership_removal(store):
    first = store.accounts.upsert_account('first','c','brand',False)
    main = store.accounts.upsert_account('main','c',None,True)
    a = store.playlists.upsert_playlist(first,'LM','First likes',0,'h',0)
    b = store.playlists.upsert_playlist(main,'LM','Main likes',0,'h',0)
    ar,br = store.playlists.get_playlist(a).playlist_scope_id,store.playlists.get_playlist(b).playlist_scope_id
    store.playlists.hide_playlist('LM',account_id=first)
    store.playlists.set_playlist_group('LM','Personal',account_id=main)
    store.overlaps.ignore_cleanup(ar,'empty',1)
    store.overlaps.ignore_overlap_playlist(br,1)
    store.playlist_recipes.set_recipe(ar,{'account':'first'},1)
    store.playlists.remove_playlist(a)
    assert store.playlists.get_hidden_playlists()=={ar}
    assert store.playlists.get_playlist_groups()=={br:'Personal'}
    assert store.overlaps.get_cleanup_ignored()=={'empty':{ar}}
    restored = store.playlists.upsert_playlist(first,'LM','First likes again',0,'h',2)
    assert store.playlists.get_playlist(restored).playlist_scope_id==ar
    assert store.playlist_recipes.get_recipe(ar)=={'account':'first'}
    with pytest.raises(ValueError,match='Choose an account'):
        store.playlists.hide_playlist('LM')


def test_shared_curation_survives_each_account_membership_removal(store):
    first = store.accounts.upsert_account('first','c','brand',False)
    main = store.accounts.upsert_account('main','c',None,True)
    ids = [store.playlists.upsert_playlist(i,'shared','Shared',0,'h',0) for i in (first,main)]
    store.playlists.set_playlist_group('shared','Keep')
    store.overlaps.suppress_overlap('shared','other',1)
    store.overlaps.ignore_merge('other|shared',['shared','other'],1)
    for pid in ids:
        store.playlists.remove_playlist(pid)
        assert store.playlists.get_playlist_groups()=={'shared':'Keep'}
        assert store.overlaps.get_suppressed_overlap_pairs()=={frozenset(('shared','other'))}
        assert store.overlaps.get_ignored_merges()[0]['members']==['other','shared']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_saved_snapshots_and_unlikes_retain_other_accounts_and_unknown_legacy(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    album = lambda bid:dict(browse=bid,title='Same',artist='Artist')
    store.collection.add_saved_album(album('unknown'))
    store.collection.replace_saved_album_snapshots({a:[album('shared'),album('a')]},now=1)
    assert store.collection.saved_album_ids()=={'unknown','shared','a'}
    store.collection.replace_saved_album_snapshots({b:[album('shared'),album('b')]},now=2)
    assert store.collection.saved_album_ids()=={'shared','a','b'}
    store.collection.remove_saved_album('shared',account_id=b)
    assert store.collection.saved_album_ids()=={'shared','a','b'}
    store.collection.replace_saved_album_snapshots({a:[]},now=3)
    assert store.collection.saved_album_ids()=={'b'}
    assert store.conn.execute('SELECT account_id FROM saved_album_memberships').fetchone()[0]==b


def test_account_selection_uses_main_explicit_or_owner_and_never_falls_back(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    other,main = object(),object()
    clients = {a:other,b:main}
    assert client_for(store,clients)==(b,main)
    assert client_for(store,clients,account_id=a)==(a,other)
    playlist = store.playlists.upsert_playlist(a,'P','Other',0,'h',0)
    assert client_for(store,clients,playlist_id=playlist)==(a,other)
    with pytest.raises(ValueError,match='not connected'):
        client_for(store,{a:other})
    with pytest.raises(ValueError,match='another account'):
        client_for(store,clients,playlist_id=playlist,account_id=b)


def test_generated_system_context_does_not_quarantine_another_accounts_likes(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    for iid in (a,b):
        store.playlists.upsert_playlist(iid,'LM','Likes',0,'h',1)
    store.playlists.set_playlist_provenance('LM',account_id=a,origin='tuneconsole',adopted=False)
    for iid in (a,b):
        store.history.record_play_event(iid,'song|artist',None,10,playlist_ytm_id='LM',source_timezone='UTC')
    assert [r[0] for r in store.conn.execute('SELECT is_generated FROM play_events ORDER BY id')]==[1,0]
    assert store.trends.ledger_track_plays(0,20)=={'song|artist':1}
    # Recap ranges use calendar day boundaries, including the day's early events.
    assert [r['account_id'] for r in store.recap.snapshot(0,86400)['plays']]==[b]
