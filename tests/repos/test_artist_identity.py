import sqlite3

import pytest

from tests.conftest import FakeClient
from yt_playlist.core import artists
from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations import v027_artist_identity_redirects as upgrade
from yt_playlist.core.migrations import v029_artist_provisional_names as names_upgrade
from yt_playlist.core.store import Store
from yt_playlist.library.sync import refresh_playlist
from yt_playlist.rec import artist_state, model_refresh


REGISTRY = (*MIGRATIONS[:28],Migration(29,'artist_provisional_names',names_upgrade.apply))


@pytest.fixture
def store():
    s = Store(':memory:')
    s.init_schema()
    yield s
    validate_version(s.conn, len(MIGRATIONS))
    s.conn.close()


def refresh(s,video='v',provider='verified'):
    return s.tracks.upsert_track(video,'Song','Artist',None,200,
                                credits=[{'name':'Artist','id':provider}])


def mute(s,aid):
    s.rec_model.record_feedback('for_you',f'artist:@artist:{aid}','mute',now=1)
    s.discovery.mark_scanned(aid,10)


def test_first_provider_identity_keeps_provisional_id_and_feedback(store):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    aid = store.artists.track_credits(tid)[0]['artist_id']
    mute(store,aid)
    store.artist_similarity.cache_similar(aid,[['Neighbor',.8]],20)
    refresh(store)
    assert store.artists.track_credits(tid)[0]['artist_id']==aid
    assert store.artists.get_artist(aid)['provider_ids']=={'youtube':'verified'}
    assert store.tracks.identity_key_for_video('v') in store.rec_model.suppressed_keys('for_you',30)
    assert store.artist_similarity.cached_similar(aid,30)==[['Neighbor',.8]]
    assert store.conn.execute('SELECT scanned_at FROM artist_scans WHERE artist_id=?',(aid,)).fetchone()[0]==10


@pytest.mark.parametrize('other_first',[False,True])
def test_existing_provider_reconciles_state_and_old_handles_atomically(store,other_first):
    if other_first:
        known = refresh(store,'known')
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    old = store.artists.track_credits(tid)[0]['artist_id']
    if not other_first:
        known = refresh(store,'known')
    target = store.artists.track_credits(known)[0]['artist_id']
    assert old!=target
    mute(store,old)
    store.discovery.mark_scanned(target,15)
    for aid,when in [(old,20),(target,30)]:
        store.discovery.upsert_discovered_artist('Artist',1,[],[],None,when,artist_id=aid)
        store.discovery.record_discovery_impressions('artist',[f'@artist:{aid}'],'same-view',when)
        store.artist_similarity.cache_similar(aid,[['Neighbor',.8]],when)
    store.collection.add_saved_album({'browse':'album','title':'Album','artist':'Artist','credits':[]})
    store.conn.execute('UPDATE album_artists SET artist_id=?',(old,))
    store.conn.commit()
    observations = store.conn.execute('SELECT COUNT(*) FROM artist_credit_observations').fetchone()[0]
    refresh(store)
    assert store.artists.resolve(old)==target
    assert store.artists.resolve(f'@artist:{old}')==target
    assert store.artists.track_credits(tid)[0]['artist_id']==target
    assert store.tracks.identity_key_for_video('v') in store.rec_model.suppressed_keys('for_you',40)
    assert store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:]==(target,15)
    assert store.conn.execute('SELECT artist_id FROM album_artists').fetchone()[0]==target
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['found_at']==20 and candidate['offered_count']==1
    assert store.artist_similarity.cached_similar(old,40)==[['Neighbor',.8]]
    assert store.conn.execute('SELECT COUNT(*) FROM artist_credit_observations').fetchone()[0]==observations+1
    assert store.conn.execute("SELECT COUNT(*) FROM identity_state_archive WHERE kind LIKE 'artist_reconcile:%'").fetchone()[0]==6
    store.discovery.mark_scanned(old,50)
    assert store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:]==(target,50)


def test_failed_contextual_reconciliation_rolls_back_all_state(store,monkeypatch):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    old = store.artists.track_credits(tid)[0]['artist_id']
    refresh(store,'known')
    mute(store,old)
    before = list(store.conn.iterdump())
    reconcile = artists.reconcile_provisional
    def fail(*args,**kwargs):
        reconcile(*args,**kwargs)
        raise RuntimeError('interrupted artist reconciliation')
    monkeypatch.setattr(artists,'reconcile_provisional',fail)
    with pytest.raises(RuntimeError,match='interrupted artist'):
        refresh(store)
    assert list(store.conn.iterdump())==before


@pytest.mark.parametrize('via', ['track', 'release'])
@pytest.mark.parametrize('feature', ['discovery', 'recommendations'])
def test_feature_reconciliation_failure_rolls_back_ingestion_and_exact_release(store, monkeypatch, via, feature):
    tid = store.tracks.upsert_track('v','Song','Artist','Album',200,album_browse_id='album')
    old = store.artists.track_credits(tid)[0]['artist_id']
    refresh(store,'known')
    store.collection.add_saved_album({'browse':'album','title':'Album','artist':'Artist'})
    mute(store,old)
    store.discovery.upsert_discovered_artist('Artist',1,[],[],None,20,artist_id=old)
    store.discovery.record_discovery_impressions('artist',[old],'view',30)
    store.artist_similarity.cache_similar(old,[['Neighbor',.8]],20)
    store.rec_model.replace_rec_artist_vectors([(artists.token(old),b'vector')])
    store.rec_proposals.put_proposals('home',{'artist':artists.token(old)},20)
    before = list(store.conn.iterdump())
    owner, name = ((artists,'reconcile_discovery_artist') if feature=='discovery'
                   else (artist_state,'reconcile_artist'))
    original = getattr(owner,name)
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('interrupted feature reconciliation')
    monkeypatch.setattr(owner,name,interrupted)
    with pytest.raises(RuntimeError,match='interrupted feature'):
        if via=='track':
            refresh(store)
        else:
            store.collection.cache_album_tracklist('album','Album','Artist',None,[],
                credits=[{'name':'Artist','id':'verified'}])
    assert list(store.conn.iterdump())==before


@pytest.mark.parametrize('via', ['track', 'release'])
def test_artist_reconciliation_excludes_retired_candidates_and_rebuilds_artist_space(store, via):
    tid = store.tracks.upsert_track('v','Song','Artist','Album',200,album_browse_id='album')
    old = store.artists.track_credits(tid)[0]['artist_id']
    known = refresh(store,'known')
    target = store.artists.track_credits(known)[0]['artist_id']
    store.collection.add_saved_album({'browse':'album','title':'Album','artist':'Artist'})
    store.tracks.set_track_genre(tid,'Techno')
    assert model_refresh.rebuild(store,100)
    assert artists.token(old) in dict(store.rec_model.get_rec_artist_content_vectors())
    track_space = store.rec_model.content_generation()
    store.rec_proposals.put_proposals('home',{'artist':artists.token(old)},100)
    with store.conn:
        store.conn.execute("INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (1,'{}',2)")
    if via=='track':
        refresh(store)
    else:
        store.collection.cache_album_tracklist('album','Album','Artist',None,[],
            credits=[{'name':'Artist','id':'verified'}])
    assert store.artists.resolve(old)==target
    assert not store.rec_model.get_rec_artist_vectors()
    assert not store.rec_model.get_rec_artist_content_vectors()
    assert store.rec_proposals.get_proposals('home') is None
    assert store.rec_model.content_generation()==track_space
    assert store.rec_refresh.has_pending_changes()
    assert store.settings.get_setting('rec_artist_fit_inputs') is None
    assert store.conn.execute('SELECT identity_version FROM rec_model_selection_runs').fetchone()[0]==0
    prepared = model_refresh.rebuild(store,101)
    assert prepared is not None and 'rec_artist_content_vectors' in prepared['vectors']
    rebuilt = dict(store.rec_model.get_rec_artist_content_vectors())
    assert artists.token(target) in rebuilt and artists.token(old) not in rebuilt
    assert not store.rec_refresh.has_pending_changes()


def test_equal_timestamp_reconciliation_prefers_surviving_payload_and_retains_originals(store):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    old = store.artists.track_credits(tid)[0]['artist_id']
    target = store.artists.track_credits(refresh(store,'known'))[0]['artist_id']
    for aid,score,neighbor in [(target,2,'Target neighbor'),(old,1,'Old neighbor')]:
        store.discovery.upsert_discovered_artist('Artist',score,[],[],None,20,artist_id=aid)
        store.artist_similarity.cache_similar(aid,[[neighbor,.8]],20)
    refresh(store)
    assert store.discovery.get_discovered_artists()[0]['score']==2
    assert store.artist_similarity.cached_similar(target,30)==[['Target neighbor',.8]]
    archived = list(store.conn.execute('SELECT kind,payload FROM identity_state_archive '
                                      "WHERE kind LIKE 'artist_reconcile:%'"))
    assert len(archived)==4
    assert {r[0] for r in archived}=={'artist_reconcile:discovered_artists','artist_reconcile:rec_artist_similar'}


@pytest.mark.parametrize('surviving',['old','target'])
def test_retained_impressions_do_not_erase_surviving_discovery_count(store,surviving):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    old = store.artists.track_credits(tid)[0]['artist_id']
    target = store.artists.track_credits(refresh(store,'known'))[0]['artist_id']
    for aid in (old,target):
        store.discovery.upsert_discovered_artist('Artist',1,[],[],None,10,artist_id=aid)
        store.discovery.record_discovery_impressions('artist',[aid],'same-view',20)
    retired_candidate = target if surviving=='old' else old
    with store.conn:
        store.conn.execute('DELETE FROM discovered_artists WHERE artist_id=?',(retired_candidate,))
    refresh(store)
    assert store.discovery.get_discovered_artists()[0]['offered_count']==1
    assert store.conn.execute("SELECT COUNT(*) FROM discovery_visible_impressions WHERE kind='artist'").fetchone()[0]==1


def test_redirects_reject_unverified_targets_verified_sources_and_chains(store):
    old = store.artists.ensure_artist('Provisional')
    other = store.artists.ensure_artist('Other provisional')
    target = store.artists.ensure_artist('Verified',provider='youtube',provider_id='verified')
    for source,destination in [(old,other),(target,old)]:
        with pytest.raises(sqlite3.IntegrityError,match='provisional source and canonical target'):
            store.conn.execute('INSERT INTO artist_redirects VALUES (?,?,?,1)',(source,destination,'test'))
    store.conn.execute('INSERT INTO artist_redirects VALUES (?,?,?,1)',(old,target,'test'))
    with pytest.raises(sqlite3.IntegrityError,match='provisional source and canonical target'):
        store.conn.execute('INSERT INTO artist_redirects VALUES (?,?,?,1)',(other,old,'test'))
    store.conn.commit()


def test_contextual_reconciliation_preserves_all_feedback_and_similarity_edges(store):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    old = store.artists.track_credits(tid)[0]['artist_id']
    target = store.artists.track_credits(refresh(store,'known'))[0]['artist_id']
    neighbor = store.artists.ensure_artist('Neighbor')
    mute(store,old)
    store.rec_model.record_feedback('for_you',f'artist:@artist:{target}','mute',now=2)
    store.artist_similarity.cache_similar(old,[['Neighbor',.5]],10)
    store.artist_similarity.cache_similar(target,[['Neighbor',.8]],20)
    store.artist_similarity.cache_similar(neighbor,[[f'@artist:{old}',.7]],20)
    raw_feedback = list(store.conn.execute('SELECT surface,item_key,kind,created_at FROM rec_feedback'))
    refresh(store)
    assert list(store.conn.execute('SELECT surface,item_key,kind,created_at FROM rec_feedback'))==raw_feedback
    assert {r[0] for r in store.conn.execute('SELECT artist_id FROM rec_feedback')}=={target}
    edges = {tuple(r) for r in store.conn.execute('SELECT * FROM artist_similarity_targets')}
    assert edges=={(target,neighbor,.8),(neighbor,target,.7)}


def test_unverified_normalized_names_never_mutate_another_artist_state(store):
    tid = store.tracks.upsert_track('v','Song','AC/DC',None,200,
                                   credits=[{'name':'AC/DC','id':'known'}])
    known = store.artists.track_credits(tid)[0]['artist_id']
    mute(store,known)
    store.discovery.upsert_discovered_artist('AC DC',1,[],[],None,20)
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['artist_id']!=known and candidate['artist']=='AC DC'
    assert store.artists.get_artist(candidate['artist_id'])['provisional']==1
    store.discovery.mark_scanned('AC DC',30)
    scans = dict(store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans'))
    assert scans=={known:10,candidate['artist_id']:30}


def test_weaker_structured_refresh_retains_known_identity(store):
    tid = refresh(store)
    aid = store.artists.track_credits(tid)[0]['artist_id']
    mute(store,aid)
    store.tracks.upsert_track('v','Song','Artist',None,200,credits=[{'name':'Artist'}])
    assert store.artists.track_credits(tid)[0]['artist_id']==aid
    assert store.artists.track_credits(tid)[0]['browse_id']=='verified'
    assert store.tracks.identity_key_for_video('v') in store.rec_model.suppressed_keys('for_you',30)


@pytest.mark.parametrize('missing_credits',[None,[],[{}]])
def test_missing_structured_artists_keep_ordered_verified_credits(store,missing_credits):
    tid = store.tracks.upsert_track('v','Song','Artist, Collaborator',None,200,
        credits=[{'name':'Artist','id':'one'},{'name':'Collaborator','id':'two'}])
    before = store.artists.track_credits(tid)
    store.tracks.upsert_track('v','Song','Artist, Collaborator',None,200,credits=missing_credits)
    assert store.artists.track_credits(tid)==before


def test_single_existing_credit_accepts_later_browse_id_without_credit_list(store):
    tid = store.tracks.upsert_track('v','Song','Artist',None,200)
    aid = store.artists.track_credits(tid)[0]['artist_id']
    mute(store,aid)
    store.tracks.upsert_track('v','Song','Artist',None,200,artist_browse_id='verified')
    assert store.artists.track_credits(tid)[0]['artist_id']==aid
    assert store.artists.track_credits(tid)[0]['browse_id']=='verified'


@pytest.mark.parametrize('via',['release_credits','release_track'])
def test_identified_release_keeps_provisional_artist_state(store,via):
    store.collection.add_saved_album({'browse':'album','title':'Album','artist':'Artist'})
    old = store.conn.execute('SELECT artist_id FROM album_artists').fetchone()[0]
    mute(store,old)
    tid = refresh(store,'known')
    target = store.artists.track_credits(tid)[0]['artist_id']
    if via=='release_credits':
        store.collection.cache_album_tracklist('album','Album','Artist',None,[],
                                               credits=[{'name':'Artist','id':'verified'}])
    else:
        store.tracks.upsert_track('known','Song','Artist','Album',200,album_browse_id='album',
                                  credits=[{'name':'Artist','id':'verified'}])
    assert store.artists.resolve(old)==target
    assert store.conn.execute('SELECT artist_id FROM album_artists').fetchone()[0]==target
    assert store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:]==(target,10)
    assert store.tracks.identity_key_for_video('known') in store.rec_model.suppressed_keys('for_you',30)


@pytest.mark.parametrize('via',['release_credits','release_track'])
def test_identifying_shared_unresolved_release_credit_does_not_identify_other_releases(store,via):
    for browse in ('one','two','three'):
        store.collection.add_saved_album({'browse':browse,'title':'Same title','artist':'Artist'})
    old = store.conn.execute('SELECT artist_id FROM album_artists').fetchone()[0]
    mute(store,old)
    known = refresh(store,'known','first')
    target = store.artists.track_credits(known)[0]['artist_id']
    other = refresh(store,'other','second')
    second = store.artists.track_credits(other)[0]['artist_id']
    if via=='release_credits':
        store.collection.cache_album_tracklist('one','Same title','Artist',None,[],
                                               credits=[{'name':'Artist','id':'first'}])
    else:
        store.tracks.upsert_track('known','Song','Artist','Same title',200,album_browse_id='one',
                                  credits=[{'name':'Artist','id':'first'}])
    def release_artists():
        return {r[0]:r[1] for r in store.conn.execute(
            "SELECT p.provider_id,aa.artist_id FROM album_provider_ids p "
            "JOIN album_artists aa ON aa.album_id=p.album_id WHERE p.provider='youtube'")}
    assert release_artists()=={'one':target,'two':old,'three':old}
    assert store.artists.resolve(old)==old
    assert artists.ambiguous_context(store.conn,old)
    # Later identification of every remaining release does not retroactively
    # resolve the earlier shared-name feedback as either verified homonym.
    for browse,provider in [('two','second'),('three','first')]:
        store.collection.cache_album_tracklist(browse,'Same title','Artist',None,[],
                                               credits=[{'name':'Artist','id':provider}])
    assert release_artists()=={'one':target,'two':second,'three':target}
    assert store.artists.resolve(old)==old
    assert store.conn.execute('SELECT artist_id FROM rec_feedback').fetchone()[0]==old
    assert store.conn.execute('SELECT scanned_at FROM artist_scans WHERE artist_id=?',(old,)).fetchone()[0]==10
    # A later exact upload can also be identified without promoting this ambiguous state.
    unknown = store.tracks.upsert_track('unknown','Unknown','Artist',None,200)
    assert store.artists.track_credits(unknown)[0]['artist_id']==old
    store.tracks.upsert_track('unknown','Unknown','Artist',None,200,
                              credits=[{'name':'Artist','id':'third'}])
    assert store.artists.track_credits(unknown)[0]['artist_id'] not in {old,target,second}
    assert store.artists.resolve(old)==old


def test_retired_provisional_name_does_not_adopt_a_new_unidentified_upload(store):
    old_entry = store.tracks.upsert_track('old','Old','Artist',None,200)
    old = store.artists.track_credits(old_entry)[0]['artist_id']
    known = refresh(store,'known')
    target = store.artists.track_credits(known)[0]['artist_id']
    refresh(store,'old')
    assert store.artists.resolve(old)==target
    unknown = store.tracks.upsert_track('unknown','Unknown','Artist',None,200)
    candidate = store.artists.track_credits(unknown)[0]['artist_id']
    assert candidate not in {old,target}
    assert store.artists.get_artist(candidate)['provisional']==1


def test_two_verified_homonyms_are_not_merged_on_a_changed_credit(store):
    tid = refresh(store,provider='one')
    old = store.artists.track_credits(tid)[0]['artist_id']
    mute(store,old)
    other = refresh(store,'known',provider='two')
    target = store.artists.track_credits(other)[0]['artist_id']
    refresh(store,provider='two')
    assert store.artists.track_credits(tid)[0]['artist_id']==target
    assert store.artists.resolve(old)==old
    assert not store.conn.execute('SELECT * FROM artist_redirects').fetchall()
    assert store.tracks.identity_key_for_video('v') not in store.rec_model.suppressed_keys('for_you',40)


def test_legacy_upgrade_then_first_structured_sync_preserves_mute_and_reopens(tmp_path):
    path = tmp_path/'state.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:20])
    s.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|artist')")
    s.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES ('v',1,'Song','Artist')")
    s.conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
    s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('for_you','artist:Artist','mute',1)")
    s.conn.execute("INSERT INTO artist_scans VALUES ('Artist',10)")
    s.conn.commit()
    migrate(s.conn,registry=REGISTRY)
    s._bind_repositories()
    old = s.artists.track_credits(1)[0]['artist_id']
    iid = s.identities.upsert_identity('main','credential',None,True)
    client = FakeClient(tracks={'P':[{'videoId':'v','title':'Song','duration_seconds':200,
                                      'artists':[{'name':'Artist','id':'verified'}]}]})
    refresh_playlist(s,iid,client,'P','Mix',20)
    assert s.artists.track_credits(1)[0]['artist_id']==old
    assert 'song|artist' in s.rec_model.suppressed_keys('for_you',30)
    assert s.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:]==(old,10)
    upgrade.validate(s.conn)
    before = list(s.conn.iterdump())
    s.conn.close()
    s = Store(path)
    migrate(s.conn,registry=REGISTRY)
    assert list(s.conn.iterdump())==before
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    s.conn.close()
    assert list((tmp_path/'backups').glob('*.db'))


def test_failed_additive_upgrade_restores_installed_version_and_verified_backup(tmp_path):
    path = tmp_path/'state.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:26])
    s.conn.execute("INSERT INTO artists(name,provisional) VALUES ('Artist',1)")
    s.conn.commit()
    before = list(s.conn.iterdump())
    def fail(conn,context):
        upgrade.apply(conn,context)
        raise RuntimeError('interrupted redirect migration')
    with pytest.raises(MigrationError,match='interrupted redirect'):
        migrate(s.conn,registry=(*MIGRATIONS[:26],Migration(27,'artist_identity_redirects',fail)))
    assert list(s.conn.iterdump())==before
    backups = list((tmp_path/'backups').glob('*.db'))
    assert backups
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==26
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    s.conn.close()


def test_installed_artist_handles_survive_reconciliation_and_reopening(tmp_path):
    path = tmp_path/'state.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:26])
    s._bind_repositories()
    tid = s.tracks.upsert_track('v','Song','Artist',None,200)
    old = s.artists.track_credits(tid)[0]['artist_id']
    target = s.artists.track_credits(refresh(s,'known'))[0]['artist_id']
    mute(s,old)
    s.conn.close()
    s = Store(path)
    migrate(s.conn,registry=REGISTRY)
    s._bind_repositories()
    refresh(s)
    before = list(s.conn.iterdump())
    s.conn.close()
    s = Store(path)
    migrate(s.conn,registry=REGISTRY)
    s._bind_repositories()
    upgrade.validate(s.conn)
    assert list(s.conn.iterdump())==before
    assert s.artists.resolve(old)==target
    assert s.tracks.identity_key_for_video('v') in s.rec_model.suppressed_keys('for_you',30)
    s.discovery.mark_scanned(old,50)
    assert s.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:]==(target,50)
    s.conn.close()
