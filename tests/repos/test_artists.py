import pytest

from tests.conftest import FakeClient
from yt_playlist.core import artist_credits
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.library.sync import refresh_playlist
from yt_playlist.rec import artist_model


def test_playlist_and_album_import_keep_all_ordered_credits(store):
    identity = store.accounts.upsert_account('main','credential',None,True)
    source = {'videoId':'v','title':'Song','artists':[{'name':'First','id':'UC1'},
              {'name':'Second, with comma','id':'UC2'}], 'duration_seconds':200}
    client = FakeClient(tracks={'P':[source]})
    refresh_playlist(store,identity,client,'P','Mix',1)
    entry = store.conn.execute("SELECT id FROM catalog_tracks WHERE video_id='v'").fetchone()[0]
    credits = store.artists.track_credits(entry)
    assert [(c['credit'],c['browse_id'],c['position']) for c in credits] == [
        ('First','UC1',0),('Second, with comma','UC2',1)]
    remember_album(store,'album',{'title':'Album','artists':source['artists'],'tracks':[source]})
    assert store.artists.track_credits(entry) == credits
    assert store.conn.execute('SELECT artist FROM catalog_tracks').fetchone()[0] == 'First, Second, with comma'
    assert len(store.collection.collection_artists()) == 2
    assert {a['browse'] for a in store.collection.collection_artists()} == {'UC1','UC2'}


def test_provider_ids_keep_homonyms_distinct_and_aliases_stable(store):
    a = store.tracks.upsert_track('a','A','Same',None,None,credits=[{'name':'Same','id':'artist-a'}])
    b = store.tracks.upsert_track('b','B','Same',None,None,credits=[{'name':'Same','id':'artist-b'}])
    a_id = store.artists.track_credits(a)[0]['artist_id']
    b_id = store.artists.track_credits(b)[0]['artist_id']
    assert a_id != b_id
    assert store.artists.resolve('Same') is None
    store.tracks.upsert_track('a','A','New spelling',None,None,
                             credits=[{'name':'New spelling','id':'artist-a'}])
    assert store.artists.track_credits(a)[0]['artist_id'] == a_id
    assert store.artists.resolve('New spelling') == a_id
    store.discovery.mark_scanned(a_id,10)
    store.artist_similarity.cache_similar(a_id,[['Neighbor A',.9]],10)
    store.artist_similarity.cache_similar(b_id,[['Neighbor B',.8]],20)
    assert store.artist_similarity.cached_similar(a_id,30) == [['Neighbor A',.9]]
    assert store.artist_similarity.cached_similar(b_id,30) == [['Neighbor B',.8]]
    assert store.artist_similarity.cached_similar('Same',30) is None
    store.discovery.upsert_discovered_artist('Same',1,[],[],None,10,artist_id=a_id)
    store.discovery.upsert_discovered_artist('Same',2,[],[],None,20,artist_id=b_id)
    assert len(store.discovery.get_discovered_artists()) == 2
    store.discovery.mark_offered('artist',[a_id],30)
    counts = {r['artist_id']:r['offered_count'] for r in store.discovery.get_discovered_artists()}
    assert counts == {a_id:1,b_id:0}
    store.discovery.record_discovery_impressions('artist',[f'@artist:{b_id}'],'page',35)
    counts = {r['artist_id']:r['offered_count'] for r in store.discovery.get_discovered_artists()}
    assert counts == {a_id:1,b_id:1}
    store.discovery.upsert_discovered_album('release-a','Same','A',None,None,30,artist_id=a_id)
    store.discovery.upsert_discovered_album('release-b','Same','B',None,None,30,artist_id=b_id)
    assert set(store.discovery.discovered_albums_for_artist(a_id)) == {'release-a'}
    assert set(store.discovery.discovered_albums_for_artist(b_id)) == {'release-b'}
    store.rec_model.record_feedback('for_you',f'artist:@artist:{a_id}','mute',now=40)
    blocked = store.rec_model.suppressed_keys('for_you',50)
    keys = {r['video_id']:r['identity_key'] for r in store.conn.execute('SELECT * FROM catalog_tracks')}
    assert keys['a'] in blocked and keys['b'] not in blocked


def test_model_uses_credits_and_preserves_user_correction(store):
    one = store.tracks.upsert_track('a','A','AC/DC',None,None,artist_browse_id='one')
    two = store.tracks.upsert_track('b','B','AC DC',None,None,artist_browse_id='two')
    store.tracks.set_track_genre(one,'Rock')
    store.tracks.set_track_genre(two,'Techno')
    first = store.artists.track_credits(one)[0]['artist_id']
    second = store.artists.track_credits(two)[0]['artist_id']
    profiles = artist_model.artist_content_profiles(store)
    assert set(profiles) == {f'@artist:{first}',f'@artist:{second}'}
    assert profiles[f'@artist:{first}']['families'] != profiles[f'@artist:{second}']['families']
    store.tracks.set_track_artist(one,'My correction')
    store.tracks.upsert_track('a','A','Provider credit',None,None,credits=[{'name':'Provider credit','id':'one'}])
    assert store.conn.execute('SELECT artist,orig_artist FROM catalog_tracks WHERE id=?', (one,)).fetchone()[:] == ('My correction','AC/DC')
    assert store.artists.track_credits(one)[0]['artist_id'] == first
    store.tracks.reset_track_artist(one)
    assert store.conn.execute('SELECT artist FROM catalog_tracks WHERE id=?', (one,)).fetchone()[0] == 'AC/DC'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
