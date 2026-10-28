"""Remote mutations and their local memberships must agree on the destination account."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.app import create_app
from yt_playlist.rec import road_trip


class Client(FakeClient):
    def get_album(self,browse_id):
        return dict(audioPlaylistId='AUDIO',title='Album',artists=[{'name':'Artist'}])

    def rate_playlist(self,playlist_id,rating):
        self.rated.append((playlist_id,rating))


@pytest.mark.parametrize('order',['main-last','main-first','main-missing'])
@pytest.mark.parametrize('operation',['home','cluster','road-trip','album','discovery'])
def test_global_mutations_keep_the_configured_main_account(store,monkeypatch,order,operation):
    other = store.identities.upsert_identity('Other','c','brand',False)
    main = store.identities.upsert_identity('Main','c',None,True)
    remote_other,remote_main = Client(),Client()
    mapping = {other:remote_other,main:remote_main}
    if order=='main-first':
        mapping = {main:remote_main,other:remote_other}
    elif order=='main-missing':
        mapping.pop(main)
    for iid in (main,other):
        store.playlists.upsert_playlist(iid,'LM','Likes',0,'h',1)
    store.tracks.upsert_track('video','Song','Artist','Album',120)
    key = store.tracks.identity_key_for_video('video')
    track = dict(video_id='video',title='Song',artist='Artist',album='Album')
    if operation=='home':
        url,data = '/home/generate',dict(name='Mix',tracks=json.dumps([track]))
    elif operation=='cluster':
        url,data = '/clusters/save',dict(name='Mix',keep_keys=json.dumps([key]))
    elif operation=='road-trip':
        rid = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],[],30,1)
        state = dict(picked=[track],own_pct=50,familiarity_pct=50,target_minutes=30,stats={})
        store.road_trip.save_road_trip_draft(rid,state,1)
        monkeypatch.setattr(road_trip,'normalized',lambda state,*args:state)
        monkeypatch.setattr(road_trip,'draft_tracks',lambda state:state['picked'])
        url,data = f'/road_trip/draft/{rid}/save',{}
    elif operation=='album':
        store.collection.add_saved_album(dict(browse='ALBUM',title='Album'),identity_id=other)
        url,data = '/collection/save-album',dict(browse_id='ALBUM')
    else:
        key = store.discovery.upsert_discovered_track('new|artist','new','New','Artist',None,None,None,None,'radio:seed',1)
        url,data = '/discovery/add',dict(kind='track',id=key)
    with TestClient(create_app(store,lambda:mapping,background_work=False,now_fn=lambda:1),
                    base_url='http://127.0.0.1') as http:
        response = http.post(url,data=data)
        assert response.status_code in {200,422,503}
        assert not remote_other.created and not remote_other.rated
        if order=='main-missing':
            assert not remote_main.created and not remote_main.rated
            assert 'connect' in response.text.lower()
        elif operation in {'home','cluster','road-trip'}:
            assert remote_main.created
            created = [p for p in store.playlists.get_playlists() if p.ytm_playlist_id!='LM']
            assert created and all(p.identity_id==main for p in created)
        elif operation=='album':
            assert remote_main.rated==[('AUDIO','LIKE')]
            owners = {r[0] for r in store.conn.execute('SELECT identity_id FROM album_library_memberships')}
            assert owners=={other,main}
            # With two owners, removing an album requires choosing the account.
            # An ambiguous request must preserve both local and remote memberships.
            assert http.post('/collection/unsave-album',data=data).status_code==422
            assert remote_main.rated==[('AUDIO','LIKE')] and not remote_other.rated
            assert {r[0] for r in store.conn.execute(
                'SELECT identity_id FROM album_library_memberships')}==owners
            assert http.post('/collection/unsave-album',data={**data,'identity_id':main}).status_code==200
            assert remote_main.rated==[('AUDIO','LIKE'),('AUDIO','INDIFFERENT')]
            assert not remote_other.rated
            assert store.collection.get_saved_albums(identity_id=other)
            assert not store.collection.get_saved_albums(identity_id=main)
        else:
            assert remote_main.rated==[('new','LIKE')]
            row = store.conn.execute("SELECT identity_id FROM item_ratings WHERE video_id='new'").fetchone()
            assert row[0]==main


@pytest.mark.parametrize('order',['main-last','main-first','main-missing'])
def test_radio_creation_uses_the_main_account(store,monkeypatch,order):
    from yt_playlist.web import radio_session
    from yt_playlist.library import playlist_generated
    other = store.identities.upsert_identity('Other','c','brand',False)
    main = store.identities.upsert_identity('Main','c',None,True)
    remotes = {other:object(),main:object()}
    clients = dict(reversed(list(remotes.items()))) if order=='main-first' else dict(remotes)
    if order=='main-missing':
        clients.pop(main)
    class ConnectedBridge(Bridge):
        @property
        def connected(self):
            return True

    ctx = SimpleNamespace(store=store,client_provider=lambda:clients,now_fn=lambda:1,
                          bridge=ConnectedBridge())
    radio = radio_session.PlaylistRadio(ctx)
    monkeypatch.setattr(radio,'join',lambda:None)
    song = dict(video_id='song',title='Song',artist='Artist')
    monkeypatch.setattr(radio_session,'RadioCandidates',lambda *args:SimpleNamespace(
        by_video={'song':song},seed_offer=lambda n:[song], tuning=radio_session.radio_tuning.DEFAULTS.copy()))
    monkeypatch.setattr(radio.policy,'pick',lambda session:song)
    writes = []
    def create(store,title,tracks,client,now,identity):
        writes.append((identity,client))
        raise RuntimeError('remote creation unavailable')
    monkeypatch.setattr(playlist_generated,'create_generated_playlist',create)
    try:
        result = radio.start()
        assert not result['ok']
        if order=='main-missing':
            assert not writes
            assert 'not connected' in result['reason']
        else:
            assert writes==[(main,remotes[main])]
            assert 'remote creation unavailable' in result['reason']
    finally:
        radio.work.stop_executor()
