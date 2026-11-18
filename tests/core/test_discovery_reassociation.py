import sqlite3

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.core.catalog_query import playable_tracks_sql
from yt_playlist.rec.onboarding import library_sample


def subject(s,entry):
    return s.tracks.catalog_subject(entry)


def move(s,video,entry,now=10):
    s.tracks.associate_youtube_item(video,subject(s,entry)['recording_id'],
                                   source='verified',evidence={'review':'confirmed identity'},now=now)


def discover(s,video,source,now):
    return s.discovery.upsert_discovered_track(video+'|artist',video,video,'Artist',None,None,
                                               None,None,source,now)


@pytest.mark.parametrize('representative_moves',[True,False])
def test_partial_move_preserves_counters_and_independent_sources_through_expiry(store,representative_moves):
    s = store
    a = s.tracks.upsert_track('a','a','Artist',None,200)
    b = s.tracks.upsert_track('b','b','Artist',None,200)
    target = s.tracks.upsert_track('target','target','Artist',None,200)
    move(s,'b',a)
    for video,token,now in [('a','radio:a',1),('b','genre:b',2)]:
        discover(s,video,token,now)
    old = subject(s,a)['recording_id']
    key = subject(s,a)['identity_key']
    s.discovery.mark_offered('track',[key],3)
    s.conn.execute('UPDATE discovery_memberships SET entry_id=? WHERE recording_id=?',
                   (a if representative_moves else b,old))
    s.conn.commit()
    move(s,'a',target,4)
    rows = {r['recording_id']:dict(r) for r in s.conn.execute('SELECT * FROM discovery_memberships')}
    new = subject(s,target)['recording_id']
    assert rows[old]['entry_id']==b
    assert (rows[old]['offered_count'],rows[old]['first_shown'],rows[old]['last_shown'])==(1,3,3)
    assert rows[new]['entry_id']==a and rows[new]['offered_count']==0
    assert rows[new]['first_shown'] is None
    assert s.discovery.discovery_sources_for_track(key)[0]['source_token']=='genre:b'
    newkey = subject(s,target)['identity_key']
    assert s.discovery.discovery_sources_for_track(newkey)[0]['source_token']=='radio:a'
    # Refresh one branch, expire the other, then let the surviving branch age out.
    discover(s,'a','radio:a',50*86400)
    assert s.discovery.gc_discovery_pool(50*86400,30,set())['tracks']==1
    assert [r['recording_id'] for r in s.discovery.get_discovered_tracks()]==[new]
    assert s.discovery.gc_discovery_pool(81*86400,30,set())['tracks']==1
    assert not s.discovery.get_discovered_tracks()
    assert len(s.conn.execute('SELECT * FROM youtube_items').fetchall())==3
    assert subject(s,b)['recording_id']==old


def test_complete_move_merges_counts_and_preserves_source_contributions(store):
    s = store
    a = s.tracks.upsert_track('a','a','Artist',None,200)
    target = s.tracks.upsert_track('target','target','Artist',None,200)
    oldkey = discover(s,'a','radio:same',1)
    newkey = discover(s,'target','radio:same',2)
    s.discovery.mark_offered('track',[oldkey],3)
    s.discovery.mark_offered('track',[newkey],4)
    move(s,'a',target,5)
    row = s.discovery.get_discovered_tracks()[0]
    assert (row['offered_count'],row['first_shown'],row['last_shown'])==(2,3,4)
    sources = s.discovery.discovery_sources_for_track(newkey)
    assert {(r['input_video_id'],r['found_at'],r['last_found_at']) for r in sources}=={('a',1,1),('target',2,2)}
    assert s.discovery.gc_discovery_pool(31*86400,30,set())['tracks']==1
    assert not s.conn.execute('SELECT * FROM discovery_memberships').fetchall()


def test_same_timestamp_moves_preserve_each_source_input(store):
    s = store
    a = s.tracks.upsert_track('a','a','Artist',None,200)
    s.tracks.upsert_track('b','b','Artist',None,200)
    target = s.tracks.upsert_track('target','target','Artist',None,200)
    move(s,'b',a)
    oldkey = discover(s,'a','radio:same',1)
    newkey = discover(s,'target','radio:same',2)
    s.discovery.mark_offered('track',[oldkey],3)
    s.discovery.mark_offered('track',[newkey],4)
    move(s,'a',target,10)
    discover(s,'b','radio:same',5)
    move(s,'b',target,10)
    assert {(row['input_video_id'], row['found_at'], row['last_found_at'])
            for row in s.discovery.discovery_sources_for_track(newkey)} == {('a', 1, 1), ('b', 5, 5), ('target', 2, 2)}
    assert len(s.discovery.get_discovered_tracks())==1
    assert s.discovery.get_discovered_tracks()[0]['offered_count']==2


def test_unknown_source_and_offer_state_stay_with_partial_original(store):
    s = store
    a = s.tracks.upsert_track('a','a','Artist',None,200)
    b = s.tracks.upsert_track('b','b','Artist',None,200)
    target = s.tracks.upsert_track('target','target','Artist',None,200)
    move(s,'b',a)
    discover(s,'a','radio:unknown',1)
    s.conn.execute('UPDATE discovery_sources SET input_video_id=NULL')
    s.conn.commit()
    move(s,'a',target)
    row = s.discovery.get_discovered_tracks()[0]
    assert row['recording_id']==subject(s,b)['recording_id'] and row['id']==b
    assert s.discovery.discovery_sources_for_track(row['identity_key'])[0]['input_video_id'] is None


def test_partial_move_keeps_completed_owners_when_final_invalidation_fails(store,monkeypatch):
    a = store.tracks.upsert_track('a','a','Artist',None,200)
    b = store.tracks.upsert_track('b','b','Artist',None,200)
    target = store.tracks.upsert_track('target','target','Artist',None,200)
    move(store,'b',a)
    discover(store,'a','radio:seed',1)
    previous = subject(store, a)['recording_id']
    recording = subject(store, target)['recording_id']
    def fail():
        assert not store.conn.in_transaction
        raise RuntimeError('interrupted association')
    monkeypatch.setattr(store.recording_keys,'invalidate_catalog',fail)
    with pytest.raises(RuntimeError,match='interrupted association'):
        move(store,'a',target)
    assert not store.conn.in_transaction
    assert subject(store, a)['recording_id'] == recording
    assert subject(store, b)['recording_id'] == previous
    memberships = {row['recording_id']: row['entry_id'] for row in store.conn.execute('SELECT * FROM discovery_memberships')}
    assert memberships == {previous: b, recording: a}
    assert store.conn.execute("SELECT recording_id FROM discovery_sources WHERE input_video_id='a'").fetchone()[0] == recording
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_membership_and_entry_guards_reject_inconsistent_writes(store):
    a = store.tracks.upsert_track('a','a','Artist',None,200)
    b = store.tracks.upsert_track('b','b','Artist',None,200)
    wrong = subject(store,b)['recording_id']
    with pytest.raises(sqlite3.IntegrityError,match='another recording'):
        store.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id) VALUES (?,?)',(wrong,a))
    store.conn.rollback()
    discover(store,'a','radio:a',1)
    with pytest.raises(sqlite3.IntegrityError,match='another recording'):
        store.conn.execute('UPDATE discovery_memberships SET entry_id=?',(b,))
    store.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError,match='Replace discovery representative'):
        store.conn.execute('UPDATE catalog_entries SET recording_id=? WHERE id=?',(wrong,a))
    store.conn.rollback()


def test_playback_consumers_take_metadata_from_one_available_upload(store):
    s = store
    iid = s.accounts.upsert_account('main','credential',None,True)
    a = s.tracks.upsert_track('a','A title','Artist','A release',100,False,thumbnail='a-cover')
    b = s.tracks.upsert_track('b','Z title','Artist','Z release',300,True,
                              thumbnail='z-cover',album_browse_id='z-release')
    s.tracks.upsert_track(None,'Unresolved','Artist','Missing',100)
    s.tracks.upsert_track('unavailable','Unavailable','Artist',None,100,False)
    move(s,'b',a)
    key = subject(s,a)['identity_key']
    s.history.record_history_plays(iid,86400,[key])
    for query,kind in [('Z title','songs'),('Artist','songs')]:
        section = next(r for r in s.search.omni_search(query)['sections'] if r['kind']==kind)
        assert [(r['label'],r['href'],r['thumbnail']) for r in section['rows']]==[
            ('Z title','/track/b','z-cover')]
    assert library_sample(s)==[{'video_id':'b','title':'Z title','artist':'Artist','album':'Z release',
                               'thumbnail':'z-cover','key':key,'duration':300}]
    assert s.charts.top_tracks()==[{'title':'Z title','artist':'Artist','video_id':'b','thumbnail':'z-cover','plays':1}]
    songs = s.charts.artist_songs('Artist')
    assert len(songs)==1
    assert (songs[0]['title'],songs[0]['video_id'],songs[0]['thumbnail'],songs[0]['album'],songs[0]['duration'],songs[0]['album_browse'])==(
        'Z title','b','z-cover','Z release',300,'z-release')


def test_scoped_representative_filters_artist_release_and_account_before_selection(store):
    s = store
    a = s.tracks.upsert_track('a','First','First artist','First release',100,True,album_browse_id='first')
    b = s.tracks.upsert_track('b','Second tune','Second artist','Second release',200,True,album_browse_id='second')
    move(s,'b',a)
    assert s.charts.artist_songs('Second artist')[0]['video_id']=='b'
    for query,kind in [('Second artist','songs'),('Second tune','songs')]:
        sections = s.search.omni_search(query)['sections']
        rows = next(section['rows'] for section in sections if section['kind']==kind)
        assert [(r['label'],r['href'],r['thumbnail']) for r in rows]==[
            ('Second tune','/track/b',None)]
    for field,value in [('artist','Second artist'),('album_browse_id','second')]:
        sql = playable_tracks_sql(f'SELECT * FROM tracks WHERE {field}=?')
        assert [r['video_id'] for r in s.conn.execute(sql,(value,))]==['b']
    iid = s.accounts.upsert_account('main','credential',None,True)
    other = s.accounts.upsert_account('other','credential',None,False)
    for owner,entry in [(iid,a),(other,b)]:
        pid = s.playlists.upsert_playlist(owner,'LM','Liked',1,'hash',1)
        s.playlists.set_playlist_tracks(pid,[entry])
    sql = playable_tracks_sql('SELECT t.* FROM tracks t WHERE EXISTS('
                              'SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
                              'WHERE pt.track_id=t.id AND p.account_id=?)')
    assert [r['video_id'] for r in s.conn.execute(sql,(other,))]==['b']
    assert [r['video_id'] for r in s.conn.execute(sql,(iid,))]==['a']
