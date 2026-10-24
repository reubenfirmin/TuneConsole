"""A rating confirmed during a remote snapshot survives its later publication."""
import pytest

from tests.conftest import FakeClient
from yt_playlist.library import ratings, sync


def _track(status):
    return {'videoId':'a','title':'Song','artists':[{'name':'Artist'}],
            'duration_seconds':200,'likeStatus':status}


def _client(status, *, partial=False, during_read=None):
    client = FakeClient(playlists=[{'playlistId':'P','title':'Mix','count':1},
                                   {'playlistId':'LM','title':'Liked','count':int(status=='LIKE')}],
        tracks={'P':[_track(status)],'LM':[_track('LIKE')] if status=='LIKE' else []})
    original = client.get_playlist
    def get_playlist(pid, **kwargs):
        detail = original(pid,**kwargs)
        if pid=='P':
            if during_read:
                during_read()
            if partial:
                detail['continuations'] = [{'unread':'page'}]
        return detail
    client.get_playlist = get_playlist
    return client


def _liked_videos(store, owner):
    lm = next(p for p in store.playlists.get_playlists() if p.identity_id==owner and p.ytm_playlist_id=='LM')
    return {r[1] for r in store.playlists.get_playlist_tracks_with_meta(lm.id)}


@pytest.mark.parametrize('partial', [False,True])
@pytest.mark.parametrize('observed,confirmed', [
    ('INDIFFERENT','LIKE'),('DISLIKE','LIKE'),('LIKE','INDIFFERENT'),('LIKE','DISLIKE')])
def test_stale_snapshot_cannot_replace_a_concurrent_confirmed_action(store, partial, observed, confirmed):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    tid = store.tracks.upsert_track('a','Song','Artist',None,200)
    lm = store.playlists.upsert_playlist(owner,'LM','Liked',0,'h',1)
    if observed=='LIKE':
        store.playlists.set_playlist_tracks(lm,[tid])
    action = lambda: ratings.record_confirmed(store,owner,'a',confirmed,110)
    result = sync.sync_identity(store,owner,_client(observed,partial=partial,during_read=action),now=100)
    # The confirmed action changed LM while its snapshot was in flight. Even a
    # fully paginated read must report incomplete evidence after that guard wins.
    assert result['resources']['ratings']['status']==('skipped' if observed=='INDIFFERENT' else 'partial')
    liked_snapshot = result['resources']['playlists']['items']['LM']
    assert liked_snapshot['status']=='partial'
    assert 'changed locally' in liked_snapshot['message']
    assert store.tracks.item_rating(owner,'a')=={'status':confirmed,'source':'api','observed_at':110}
    key = store.tracks.identity_key_for_video('a')
    assert (key in store.rec_model.recent_liked_keys())==(confirmed=='LIKE')
    assert (key in store.rec_model.disliked_identity_keys())==(confirmed=='DISLIKE')
    assert _liked_videos(store,owner)==({'a'} if confirmed=='LIKE' else set())
    # A subsequent complete snapshot establishes a withdrawal, or confirms an existing action
    # without changing its original provenance/time.
    sync.sync_identity(store,owner,_client('INDIFFERENT'),now=120)
    expected = {'status':'INDIFFERENT','source':'api','observed_at':110} if confirmed=='INDIFFERENT' else {
        'status':'INDIFFERENT','source':'sync','observed_at':120}
    assert store.tracks.item_rating(owner,'a')==expected
    assert key not in store.rec_model.recent_liked_keys()
    assert key not in store.rec_model.disliked_identity_keys()
    assert _liked_videos(store,owner)==set()


@pytest.mark.parametrize('source', ['api','action'])
def test_sync_confirmation_keeps_action_time_and_ties_favor_confirmed_actions(store, source):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    store.tracks.record_item_rating(owner,'a','LIKE',source=source,now=110)
    # Older and equal-time conflicting snapshots cannot erase an action.
    for when in (100,110):
        store.tracks.record_item_rating(owner,'a','DISLIKE',source='sync',now=when)
    store.tracks.record_item_rating(owner,'a','LIKE',source='sync',now=120)
    assert store.tracks.item_rating(owner,'a')=={'status':'LIKE','source':source,'observed_at':110}
    store.tracks.record_item_rating(owner,'b','DISLIKE',source='sync',now=110)
    store.tracks.record_item_rating(owner,'b','LIKE',source=source,now=110)
    assert store.tracks.item_rating(owner,'b')=={'status':'LIKE','source':source,'observed_at':110}


def test_equal_time_sync_conflicts_retain_existing_evidence_and_newer_remote_changes_win(store):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    store.tracks.record_item_rating(owner,'a','LIKE',source='sync',now=10)
    store.tracks.record_item_rating(owner,'a','DISLIKE',source='sync',now=10)
    assert store.tracks.item_rating(owner,'a')['status']=='LIKE'
    store.tracks.record_item_rating(owner,'a','DISLIKE',source='sync',now=11)
    assert store.tracks.item_rating(owner,'a')=={'status':'DISLIKE','source':'sync','observed_at':11}


def test_rejected_confirmed_input_projects_the_winning_rating_to_liked_music(store):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    store.tracks.upsert_track('a','Song','Artist',None,200)
    store.playlists.upsert_playlist(owner,'LM','Liked',0,'h',1)
    ratings.record_confirmed(store,owner,'a','LIKE',20)
    ratings.record_confirmed(store,owner,'a','DISLIKE',10)
    key = store.tracks.identity_key_for_video('a')
    assert store.tracks.item_rating(owner,'a')=={'status':'LIKE','source':'api','observed_at':20}
    assert _liked_videos(store,owner)=={'a'}
    assert key in store.rec_model.recent_liked_keys()
    assert key not in store.rec_model.disliked_identity_keys()
