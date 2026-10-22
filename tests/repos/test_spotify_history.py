import json
import sqlite3

import pytest

from yt_playlist.core import artists, listening
from yt_playlist.library.spotify_data import import_spotify_data
from yt_playlist.library.takeout import seed_discovery_from_unmatched
from yt_playlist.rec import discover, graduation, rec_params, transient
from yt_playlist.web.context import Ctx


def test_removal_preserves_shared_sources_other_accounts_and_independent_days(store):
    one = store.identities.upsert_identity('main','bridge',None,True)
    two = store.identities.upsert_identity('second','bridge2',None,False)
    shared, only, other = ('shared',None,1700000000), ('spotify-only',None,1700000100), ('other',None,1700000200)
    store.history.import_listening_events(one,[shared,only],source='spotify_export')
    store.history.import_listening_events(one,[shared],source='takeout')
    store.history.record_play_event(one,'shared','yt',shared[2])
    store.history.import_listening_events(two,[other],source='takeout')
    store.history.import_listening_events(two,[('old-alias',None,1700000300)],source='spotify')
    store.history.record_history_plays(one,1700000400,['independent'])
    assert store.history.spotify_history_stats()['events'] == 3
    report = store.history.remove_spotify_history()
    assert report['events'] == 3
    assert store.history.spotify_history_stats()['events'] == 0
    assert {r['identity_key'] for r in store.conn.execute('SELECT * FROM play_events')} == {'shared','other'}
    assert {r[0] for r in store.conn.execute('SELECT source FROM play_event_sources')} == {'live','takeout'}
    assert 'independent' in store.history.listening_day_counts(one)
    assert store.history.listening_day_counts(two) == {'other':1}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_removal_only_removes_artists_without_independent_discovery_provenance(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.discovery.upsert_discovered_artist('Shared',9,['reason'],['fit'],None,10)
    store.discovery.seed_spotify_artists(iid,{'Shared':4,'Spotify only':5},20)
    # Later independent rediscovery must also retain its own origin.
    store.discovery.seed_spotify_artists(iid,{'Later independent':3},20)
    store.discovery.upsert_discovered_artist('Later independent',7,[],[],None,30)
    store.discovery.seed_spotify_artists(iid,{'Spotify only':5},40)
    assert store.history.spotify_history_stats()['artists'] == 3
    report = store.history.remove_spotify_history()
    remaining = {r['artist']:r for r in store.discovery.get_discovered_artists()}
    assert report['artists_removed'] == 1
    assert set(remaining) == {'Shared','Later independent'}
    assert remaining['Shared']['score'] == 9 and remaining['Shared']['because'] == ['reason']
    assert store.history.spotify_history_stats()['artists'] == 0


def test_unmatched_interest_accumulates_once_across_overlapping_files(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    def payload(days):
        return json.dumps([{'ts':f'2024-01-{day:02}T10:00:00Z', 'ms_played':180000,
                           'master_metadata_track_name':'Unknown',
                           'master_metadata_album_artist_name':'Unmatched',
                           'spotify_track_uri':'spotify:track:x'} for day in days])
    import_spotify_data(store,payload([1,2]))
    report = import_spotify_data(store,payload([2,3]))
    assert report['unmatched_artists'] == {'Unmatched':3}
    assert report['listens_added'] == 1
    assert store.discovery.seed_spotify_artists(iid,report['unmatched_artists'],100) == 1
    assert import_spotify_data(store,payload([1,2,3]))['listens_added'] == 0
    assert store.history.spotify_unmatched_artists(iid) == {'Unmatched':3}
    store.tracks.upsert_track('yt','Unknown','Unmatched',None,180)
    matched = import_spotify_data(store,payload([1,2,3]))
    assert matched['events_added'] == 3 and matched['unmatched_artists'] == {}
    store.history.remove_spotify_history()
    assert not store.conn.execute('SELECT * FROM spotify_history_listens').fetchall()


def test_removal_clears_models_forecasts_and_rollbacks_but_keeps_user_choices(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.tracks.upsert_track('yt','Song','Artist',None,180)
    key = store.tracks.identity_key_for_video('yt')
    store.history.import_listening_events(iid,[(key,None,1700000000)],source='spotify_export')
    store.rec_surface.put_proposals('trend_rollups',{'private_model':'old'},100)
    store.rec_model.record_like(key,100,provenance='action')
    store.settings.set_setting('user-setting','keep')
    store.settings.set_setting('spotify_imported_at','100')
    run = store.model_selection.start_selection(100,{'status':'applied'})
    with store.conn:
        store.conn.execute("INSERT INTO rec_vectors VALUES (?,x'00000000')",(key,))
        store.conn.execute('INSERT INTO rec_model_previous_vectors SELECT ?,identity_key,vec FROM rec_vectors',(run,))
        store.conn.execute("INSERT INTO rec_model_forecasts VALUES (?,100,'{}')",(run,))
    store.history.remove_spotify_history()
    assert store.rec_model.rec_vectors_count() == 0
    assert store.rec_surface.get_proposals('trend_rollups') is None
    assert not store.model_selection.selection_runs()
    assert not store.model_selection.restore_model_selection(run,200)
    assert key in store.rec_model.recent_liked_keys()
    assert store.settings.get_setting('user-setting') == 'keep'
    assert store.settings.get_setting('spotify_imported_at') is None
    assert store.tracks.identity_key_for_video('yt') == key


@pytest.mark.parametrize('source', ['spotify', 'spotify_export', 'takeout'])
def test_imported_listening_learns_saved_preferences_without_double_counting_shared_plays(store, source):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    tid = store.tracks.upsert_track('yt','Song','Artist',None,180)
    store.tracks.set_track_genre(tid,'Techno')
    key = store.tracks.identity_key_for_video('yt')
    now = 1700000000
    rec_params.set_param(store, 'source_w_play', 1.0)
    rec_params.set_param(store, 'theme_threshold', 0.2)
    store.history.import_listening_events(iid,[(key,None,now)],source=source)
    leans = transient.play_facet_leans(store,now)
    assert leans
    graduation.graduate_play_exposure(store,now)
    weights = store.rec_model.get_weights(now=now)
    assert weights and all(weight > 1.0 for weight in weights.values())
    themes = {axis:store.rec_surface.get_theme(axis) for axis in leans}
    # Another import of the same occurrence must not amplify its evidence or learning.
    store.history.import_listening_events(iid,[(key,None,now)],source='takeout' if source != 'takeout' else 'spotify_export')
    assert store.history.recent_plays_with_ts() == [(key,float(now))]
    assert transient.play_facet_leans(store,now) == leans
    graduation.graduate_play_exposure(store,now)
    assert store.rec_model.get_weights(now=now) == weights
    assert {axis:store.rec_surface.get_theme(axis) for axis in leans} == themes


def test_removing_spotify_stops_further_learning_and_keeps_saved_preferences(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    tid = store.tracks.upsert_track('yt','Song','Artist',None,180)
    store.tracks.set_track_genre(tid,'Techno')
    key = store.tracks.identity_key_for_video('yt')
    now = 1700000000
    rec_params.set_param(store, 'source_w_play', 1.0)
    rec_params.set_param(store, 'theme_threshold', 0.2)
    store.history.import_listening_events(iid,[(key,None,now)],source='spotify_export')
    graduation.graduate_play_exposure(store,now)
    weights = store.rec_model.weight_records()
    themes = [tuple(row) for row in store.rec_surface.theme_rows()]
    assert weights

    store.history.remove_spotify_history()
    assert transient.play_facet_leans(store,now) == {}
    graduation.graduate_play_exposure(store,now + 86400)
    assert store.rec_model.weight_records() == weights
    assert [tuple(row) for row in store.rec_surface.theme_rows()] == themes


def test_artist_identity_reconciliation_preserves_removable_import_origin(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.discovery.seed_spotify_artists(iid,{'Provisional':4},10)
    # Resolving an artist against provider evidence must move its receipt and origin.
    with store.conn:
        previous = artists.lookup(store.conn,'Provisional')
        target = artists.ensure(store.conn,'Verified',provider='youtube',provider_id='UCverified')
        store.conn.execute("INSERT INTO spotify_history_listens VALUES (?,'spotify:track:x',100,NULL,?)",(iid,previous))
        artists.reconcile_provisional(store.conn,previous,target,source='youtube',now=20)
    assert store.conn.execute('SELECT artist_id FROM spotify_history_listens').fetchone()[0] == target
    assert store.history.spotify_history_stats()['artists'] == 1
    assert store.history.remove_spotify_history()['artists_removed'] == 1
    assert not store.discovery.get_discovered_artists()


def test_failed_removal_rolls_back_evidence_and_artist_changes(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.history.import_listening_events(iid,[('keep-on-failure',None,1700000000)],source='spotify_export')
    store.discovery.seed_spotify_artists(iid,{'Keep on failure':3},10)
    with store.conn:
        store.conn.execute("CREATE TRIGGER fail_removal BEFORE DELETE ON rec_proposals BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    store.rec_surface.put_proposals('trend_rollups',{'old':True},10)
    with pytest.raises(sqlite3.IntegrityError,match='injected failure'):
        store.history.remove_spotify_history()
    assert store.history.spotify_history_stats()['events'] == 1
    assert store.history.spotify_history_stats()['artists'] == 1
    assert store.rec_surface.get_proposals('trend_rollups') == {'old':True}


def test_later_takeout_interest_keeps_an_artist_after_spotify_is_removed(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.discovery.seed_spotify_artists(iid,{'Shared':5},10)
    assert seed_discovery_from_unmatched(store,{'Shared':7},20) == 0
    assert store.history.remove_spotify_history()['artists_removed'] == 0
    assert [a['artist'] for a in store.discovery.get_discovered_artists()] == ['Shared']
    assert store.discovery.get_discovered_artists()[0]['score'] == 7


def test_model_suggestions_derived_from_spotify_are_removed_or_refreshed(store):
    store.discovery.upsert_discovered_artist('Independent',9,['old independent'],[],None,10)
    store.discovery.upsert_discovered_artist('Independent',100,['Spotify bridge'],['fit'],None,20,source='spotify_model')
    store.discovery.upsert_discovered_artist('Derived only',50,['Spotify bridge'],[],None,20,source='spotify_model')
    assert store.history.spotify_history_stats()['artists'] == 2
    assert store.history.remove_spotify_history()['artists_removed'] == 1
    remaining = store.discovery.get_discovered_artists()
    assert len(remaining) == 1 and remaining[0]['artist'] == 'Independent'
    assert remaining[0]['score'] == 9 and remaining[0]['because'] == [] and remaining[0]['fits'] == []


@pytest.mark.parametrize('evidence', ['events', 'days', 'artists'])
def test_discovery_pass_keeps_spotify_derived_suggestions_removable(store, monkeypatch, evidence):
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    if evidence == 'events':
        store.history.import_listening_events(iid, [('song',None,1700000000)], source='spotify_export')
    elif evidence == 'days':
        with store.conn:
            listening.calendar(store.conn, iid, 'UTC')
            listening.observe_day(store.conn, iid, 'song', '2024-01-01', 'spotify', 'UTC', 100)
    else:
        store.discovery.seed_spotify_artists(iid, {'Imported artist':4}, 100)
    monkeypatch.setattr(discover, 'new_artists', lambda ctx: [
        {'artist':'Model suggestion', 'score':5, 'because':['Imported history'], 'fits':[]}])
    monkeypatch.setattr(discover, 'populate_discovered_tracks', lambda *args: None)
    monkeypatch.setattr(discover, 'populate_radio_tracks', lambda *args: None)
    ctx = Ctx(store=store, client_provider=lambda: {}, now_fn=lambda: 1700000000,
              templates=None, jobs=None)
    discover.run_discovery(ctx, 1700000000, budget=0)
    assert 'Model suggestion' in {a['artist'] for a in store.discovery.get_discovered_artists()}
    store.history.remove_spotify_history()
    assert not store.discovery.get_discovered_artists()
