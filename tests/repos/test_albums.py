import pytest

from yt_playlist.core import albums
from yt_playlist.core.album_queries import TRACK_LINKS


def test_releases_keep_equal_titles_separate_and_multiple_release_links(store):
    entry = store.tracks.upsert_track('v','Song','Artist','Same',10,album_browse_id='first')
    store.tracks.upsert_track('v','Song','Artist','Same',10,album_browse_id='second')
    store.collection.add_saved_album({'browse':'first','title':'Same','artist':'Artist'})
    store.discovery.upsert_discovered_album('second','Artist','Same',None,None,1)
    first,second = albums.lookup(store.conn,'first'),albums.lookup(store.conn,'second')
    assert first != second
    assert {r[0] for r in store.conn.execute('SELECT album_id FROM album_tracks WHERE entry_id=?', (entry,))} == {first,second}
    assert store.conn.execute('SELECT COUNT(*) FROM saved_album_memberships WHERE album_id=?', (first,)).fetchone()[0] == 1
    assert store.conn.execute('SELECT discovery_found_at FROM albums WHERE id=?', (second,)).fetchone()[0] == 1
    store.conn.execute('DELETE FROM discovered_albums WHERE browse_id=\'second\'')
    assert albums.lookup(store.conn,'second') == second
    assert store.conn.execute('SELECT COUNT(*) FROM album_tracks WHERE album_id=?', (second,)).fetchone()[0] == 1


def test_full_fetch_retains_order_repeats_and_disc_numbers(store):
    a = store.tracks.upsert_track('a','First','Artist',None,10)
    b = store.tracks.upsert_track('b','Second','Artist',None,10)
    store.collection.cache_album_tracklist('release', 'Album', 'Artist', None, [], entries=[
        {'entry_id':b,'disc_number':1,'track_number':1},
        {'entry_id':a,'disc_number':2,'track_number':1},
        {'entry_id':b,'disc_number':2,'track_number':2}])
    aid = albums.lookup(store.conn, 'release')
    assert list(map(tuple,store.conn.execute('SELECT entry_id,position,disc_number,track_number FROM album_tracks WHERE album_id=? ORDER BY position', (aid,)))) == [(b,0,1,1),(a,1,2,1),(b,2,2,2)]
    assert store.conn.execute('SELECT fetched_at IS NOT NULL,status,expected_count FROM album_tracklists '
                              'WHERE id=(SELECT current_tracklist_id FROM albums WHERE id=?)', (aid,)).fetchone()[:] == (
                                  1, 'complete', 3)
    assert [row[0] for row in store.conn.execute(
        'SELECT r.identity_key FROM albums a JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id '
        'JOIN recordings r ON r.id=o.recording_id WHERE a.id=? ORDER BY o.position', (aid,))] == [
            'second|artist', 'first|artist', 'second|artist']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_release_consumers_keep_same_title_releases_and_previous_associations(store):
    from random import Random
    from yt_playlist.rec.discover import _scan_artist_albums

    iid = store.accounts.upsert_account('me',None,True)
    pid = store.playlists.upsert_playlist(iid,'P','Mix',1,'h',0)
    entry = store.tracks.upsert_track('v','Song','Artist','Same',10,album_browse_id='first',artist_browse_id='UC_artist')
    store.tracks.upsert_track('v','Song','Artist','Same',10,album_browse_id='second',artist_browse_id='UC_artist')
    store.playlists.set_playlist_tracks(pid,[entry])
    store.collection.add_saved_album({'browse':'first','title':'Same','artist':'Artist'})
    store.history.import_play_events(iid,[('song|artist','v',100)])
    assert {a['browse'] for a in store.collection.collection_albums()} == {'first','second'}
    artist_albums = store.collection.artist_albums('Artist','UC_artist')
    assert {(a['browse'],a['songs'],a['plays'],a['saved']) for a in artist_albums} == {('first',1,1,True),('second',1,1,False)}
    assert store.collection.collection_artists()[0]['n_albums'] == 2
    chart = store.charts.listen_distribution('album')
    assert len(chart)==2 and set(chart.values())=={1}
    assert set(store.charts.album_browse_ids().values())=={'first','second'}
    assert store.collection.playback_source(video_id='v') is None
    assert store.collection.playback_source(video_id='v',album_browse_id='first')['browse']=='first'
    assert not any(store.collection.has_published_album_tracklist(browse) for browse in ('first', 'second'))

    _scan_artist_albums(store,'Artist',{'albums':[{'browse_id':b,'title':'Same'} for b in ('first','second','third')]},
                        store.rec_query.owned_albums(),store.collection.saved_album_ids(),None,100,5,Random(0))
    assert {a['browse_id'] for a in store.discovery.get_discovered_albums()} == {'third'}
    assert store.collection.saved_albums_recency()['first']==100
    store.discovery.upsert_discovered_album('first','Artist','Same',None,None,1)
    store.collection.remove_saved_album('first')
    assert {a['browse_id'] for a in store.discovery.get_discovered_albums()} == {'first','third'}
    assert store.collection.album_id('first') is not None


def test_full_fetch_round_trip_order_coverage_and_conflicts_follow_all_releases(store):
    from yt_playlist.library.album_catalog import remember_album

    a = store.tracks.upsert_track('a','First','Artist','Other',10,album_browse_id='other')
    b = store.tracks.upsert_track('b','Second','Artist','Album',10,album_browse_id='release')
    remember_album(store,'release',{'title':'Album','year':'2001','artists':[{'name':'Artist'}], 'trackCount':3,
        'tracks':[{'videoId':v,'title':title,'discNumber':disc,'trackNumber':track}
                  for v,title,disc,track in [('b','Second',1,1),('a','First',2,1),('b','Second',2,2)]]})
    assert [t['video_id'] for t in store.charts.album_tracks_detail('release')] == ['b','a','b']
    assert store.collection.complete_album_tracklist_ids()=={'release'}
    assert store.collection.cached_album_share('release')['video_ids']==['b','a','b']
    store.enrichment.upsert_conflict(a,'genre',[{'provider':'lastfm','value':'Rock'},{'provider':'musicbrainz','value':'Pop'}])
    for bid in ('other','release'):
        assert store.enrichment_reports.conflict_count_for_album(bid)==1
        assert store.enrichment_reports.unresolved_conflicts_for_album(bid)[0]['track_id']==a
    assert store.conn.execute("SELECT year FROM albums WHERE id=?", (store.collection.album_id('release'),)).fetchone()[0]=='2001'
    store.conn.execute('UPDATE track_sources SET library_origin=NULL,library_added_at=NULL WHERE id=?', (b,))
    store.conn.commit()
    assert store.collection.complete_album_tracklist_ids()==set()
    # Library membership is independent of the complete provider publication.
    assert store.collection.has_published_album_tracklist('release')
    assert store.collection.cached_album_share('release') == {
        'title': 'Album', 'video_ids': ['b', 'a', 'b']}


def test_recording_reassociation_preserves_release_relationships(store):
    a = store.tracks.upsert_track('a','Song','Artist','Album',10,album_browse_id='first')
    b = store.tracks.upsert_track('b','Alternate title','Artist','Album',10,album_browse_id='second')
    subject = store.tracks.catalog_subject(a)
    store.tracks.associate_youtube_item('b',subject['recording_id'],source='verified',evidence={'same_recording':True},now=1)
    assert {r[0] for r in store.conn.execute(f'SELECT browse_id FROM {TRACK_LINKS} WHERE recording_id=?',
                                            (subject['recording_id'],))} == {'first', 'second'}
    assert {t['track_id'] for t in store.charts.album_tracks_detail('second')}=={b}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_recap_uses_all_release_relationships_without_inventing_playback_context(store):
    from yt_playlist.rec.recap_metrics import month_bounds,profile

    iid = store.accounts.upsert_account('me',None,True)
    keys = []
    for i in range(3):
        entry = store.tracks.upsert_track(str(i),f'Song {i}','Artist','Album',10,album_browse_id='release')
        store.tracks.upsert_track(str(i),f'Song {i}','Artist','Other',10,album_browse_id=f'other-{i}')
        keys.append(store.tracks.catalog_subject(entry)['identity_key'])
    since,until = month_bounds('2026-09')
    data = store.recap.snapshot(since,until)
    assert all(data['meta'][k]['album_browse_ids']=={'release',f'other-{i}'} for i,k in enumerate(keys))
    assert all(data['meta'][k]['album_browse_id'] is None for k in keys)
    events = [dict(account_id=iid,source='live',identity_key=k,played_at=since+i*60) for i,k in enumerate(keys)]
    stats = profile('2026-09',events,data['meta'],{(iid,'live'):(since-1,until)},[])
    assert stats.album_covered_plays==stats.album_run_plays==3
