import json
import sqlite3

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations import v028_discovery_representatives as upgrade
from yt_playlist.core.store import Store
from yt_playlist.core.catalog_query import playable_tracks_sql
from yt_playlist.rec.onboarding import library_sample


REGISTRY = (*MIGRATIONS[:27],Migration(28,'discovery_representatives',upgrade.apply))


@pytest.fixture
def store():
    s = Store(':memory:')
    # Workflow cases call current repositories. The explicit migration cases
    # below retain their independent v027/v028 databases and registry.
    s.init_schema()
    yield s
    upgrade.validate(s.conn)
    s.conn.close()


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
    # Refresh one branch, expire the other, then remove the last surviving source.
    discover(s,'a','radio:a',50*86400)
    assert s.discovery.gc_discovery_pool(50*86400,30,set())['tracks']==1
    assert [r['recording_id'] for r in s.discovery.get_discovered_tracks()]==[new]
    assert s.discovery.remove_discovery_source(newkey,'radio:a')
    assert not s.discovery.get_discovered_tracks()
    assert len(s.conn.execute('SELECT * FROM youtube_items').fetchall())==3
    assert subject(s,b)['recording_id']==old


def test_complete_move_merges_counts_and_preserves_source_collision_evidence(store):
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
    assert len(sources)==1 and sources[0]['found_at']==1 and sources[0]['last_found_at']==2
    archived = [json.loads(r[0]) for r in s.conn.execute("SELECT payload FROM identity_state_archive WHERE kind='discovery_reassociation'")]
    assert {r['input_video_id'] for r in archived}=={'a','target'}
    assert s.discovery.remove_discovery_source(newkey,'radio:same')
    assert not s.conn.execute('SELECT * FROM discovery_memberships').fetchall()


def test_same_timestamp_moves_retain_each_original_source_input(store):
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
    archived = [json.loads(r[0]) for r in s.conn.execute(
        "SELECT payload FROM identity_state_archive WHERE kind='discovery_reassociation'")]
    assert {row['input_video_id'] for row in archived}=={'a','b','target'}
    assert len(archived)==4
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


def test_failed_partial_move_rolls_back_representative_sources_and_events(store,monkeypatch):
    a = store.tracks.upsert_track('a','a','Artist',None,200)
    b = store.tracks.upsert_track('b','b','Artist',None,200)
    target = store.tracks.upsert_track('target','target','Artist',None,200)
    move(store,'b',a)
    discover(store,'a','radio:seed',1)
    before = list(store.conn.iterdump())
    def fail(conn):
        raise RuntimeError('interrupted association')
    monkeypatch.setattr(catalog,'invalidate_derived',fail)
    with pytest.raises(RuntimeError,match='interrupted association'):
        move(store,'a',target)
    assert list(store.conn.iterdump())==before


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
    iid = s.identities.upsert_identity('main','credential',None,True)
    a = s.tracks.upsert_track('a','A title','Artist','A release',100,False,thumbnail='a-cover')
    b = s.tracks.upsert_track('b','Z title','Artist','Z release',300,True,
                              thumbnail='z-cover',album_browse_id='z-release')
    s.tracks.upsert_track(None,'Unresolved','Artist','Missing',100)
    s.tracks.upsert_track('unavailable','Unavailable','Artist',None,100,False)
    move(s,'b',a)
    key = subject(s,a)['identity_key']
    s.history.record_history_plays(iid,86400,[key])
    for query,kind in [('Z title','songs'),('Artist','tracks_by')]:
        section = next(r for r in s.search.omni_search(query)['sections'] if r['kind']==kind)
        assert [(r['label'],r['href'],r['thumbnail']) for r in section['rows']]==[
            ('Z title','/track/b','z-cover')]
    assert library_sample(s)==[{'video_id':'b','title':'Z title','artist':'Artist','album':'Z release',
                               'thumbnail':'z-cover','key':key}]
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
    for query,kind in [('Second artist','tracks_by'),('Second tune','songs')]:
        sections = s.search.omni_search(query)['sections']
        rows = next(section['rows'] for section in sections if section['kind']==kind)
        assert [(r['label'],r['href'],r['thumbnail']) for r in rows]==[
            ('Second tune','/track/b',None)]
    for field,value in [('artist','Second artist'),('album_browse_id','second')]:
        sql = playable_tracks_sql(f'SELECT * FROM tracks WHERE {field}=?')
        assert [r['video_id'] for r in s.conn.execute(sql,(value,))]==['b']
    iid = s.identities.upsert_identity('main','credential',None,True)
    other = s.identities.upsert_identity('other','credential',None,False)
    for owner,entry in [(iid,a),(other,b)]:
        pid = s.playlists.upsert_playlist(owner,'LM','Liked',1,'hash',1)
        s.playlists.set_playlist_tracks(pid,[entry])
    sql = playable_tracks_sql('SELECT t.* FROM tracks t WHERE EXISTS('
                              'SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
                              'WHERE pt.track_id=t.id AND p.identity_id=?)')
    assert [r['video_id'] for r in s.conn.execute(sql,(other,))]==['b']


def seed_installed_mismatch(s):
    """Independent SQL fixture of the installed v27 shape and its partial-move bug."""
    s.conn.executescript("""
        INSERT INTO recordings(id,identity_key) VALUES (1,'old'),(2,'new'),(3,'retired');
        INSERT INTO youtube_items(video_id,recording_id) VALUES ('a',2),('b',1),('c',2);
        INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,2,'a'),(2,1,'b'),(3,2,'c');
        INSERT INTO discovery_memberships VALUES (1,1,1,3,4,5),(2,3,2,4,5,7),(3,3,0,1,2,11);
        INSERT INTO discovery_sources VALUES
            (1,'radio:a','a',1,'radio','a',3),
            (1,'genre:b','b',2,'genre','b',4),
            (2,'radio:a','c',2,'radio','a',5),
            (3,'radio:unknown',NULL,0,'radio','unknown',2);
    """)


def test_populated_upgrade_repairs_partial_and_complete_moves_and_reopens(tmp_path):
    path = tmp_path/'state.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:27])
    seed_installed_mismatch(s)
    before = {name:[tuple(r) for r in s.conn.execute(f'SELECT * FROM {name}')] for name in
              ('recordings','youtube_items','catalog_entries')}
    migrate(s.conn,registry=REGISTRY)
    upgrade.validate(s.conn)
    members = {r['recording_id']:dict(r) for r in s.conn.execute('SELECT * FROM discovery_memberships')}
    assert members[1]['entry_id']==2 and members[1]['offered_count']==5
    assert members[2]['entry_id']==3 and members[2]['offered_count']==18
    assert (members[2]['found_at'],members[2]['first_shown'],members[2]['last_shown'])==(0,1,5)
    assert [(r['recording_id'],r['source_token'],r['found_at'],r['last_found_at'])
            for r in s.conn.execute('SELECT * FROM discovery_sources ORDER BY recording_id,source_token')]==[
                (1,'genre:b',2,4),(2,'radio:a',1,5),(2,'radio:unknown',0,2)]
    for name,rows in before.items():
        assert [tuple(r) for r in s.conn.execute(f'SELECT * FROM {name}')]==rows
    assert s.conn.execute("SELECT COUNT(*) FROM identity_state_archive WHERE kind='discovery_repair_v28'").fetchone()[0]==6
    state = list(s.conn.iterdump())
    s.conn.close()
    s = Store(path)
    migrate(s.conn,registry=REGISTRY)
    upgrade.validate(s.conn)
    assert list(s.conn.iterdump())==state
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    s.conn.close()
    assert not (tmp_path/'backups').exists()


def test_failed_upgrade_restores_installed_schema_and_rows(tmp_path):
    s = Store(tmp_path/'state.db')
    migrate(s.conn,registry=MIGRATIONS[:27])
    seed_installed_mismatch(s)
    before = list(s.conn.iterdump())
    def fail(conn,context):
        upgrade.apply(conn,context)
        raise RuntimeError('interrupted representative upgrade')
    with pytest.raises(MigrationError,match='interrupted representative'):
        migrate(s.conn,registry=(*MIGRATIONS[:27],Migration(28,'discovery_representatives',fail)))
    assert list(s.conn.iterdump())==before
    s.conn.close()


def test_upgrade_retains_crossed_source_inputs_before_merging_keys(tmp_path):
    s = Store(tmp_path/'state.db')
    migrate(s.conn,registry=MIGRATIONS[:27])
    seed_installed_mismatch(s)
    s.conn.execute("DELETE FROM discovery_sources")
    s.conn.execute("INSERT INTO discovery_sources VALUES (1,'radio:same','a',1,'radio','same',3)")
    s.conn.execute("INSERT INTO discovery_sources VALUES (2,'radio:same','b',2,'radio','same',4)")
    s.conn.commit()
    migrate(s.conn,registry=REGISTRY)
    assert [tuple(r) for r in s.conn.execute('SELECT recording_id,input_video_id,found_at,last_found_at '
                                            'FROM discovery_sources ORDER BY recording_id')]==[(1,'b',2,4),(2,'a',1,3)]
    upgrade.validate(s.conn)
    s.conn.close()
