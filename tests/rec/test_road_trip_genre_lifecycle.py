"""Current family edits through real recipe, draft, restart and reroll APIs."""
import json

import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import road_trip


def _song(store, video, genre):
    entry = store.tracks.upsert_track(video,'Song '+video,'Artist '+video,None,300,
                                     available=True,video_type='MUSIC_VIDEO_TYPE_ATV')
    store.tracks.set_track_genre(entry,genre)
    store.tracks.set_track_year(entry,2000)


def _assert_blend(state, order, banned):
    assert state['picks']==order
    assert [row['video_id'] for row in state['picked']]==order
    assert [(row['mix_source'],row['family']) for row in state['picked']]==[('blend','jazz')]*3
    assert state['banned']==banned and state['blend_available']
    stats = state['stats']
    assert [stats[name+'_count'] for name in ('own','their','yours','blend','overlap')]==[3,0,0,3,3]
    assert [stats[name+'_minutes'] for name in ('own','their','yours','blend')]==[15,0,0,15]
    assert stats['minutes']==15 and stats['mix_targets']['blend']==1
    assert not stats.get('short',{}).get('blend',0)
    assert stats['mix_targets']['blend']<=stats['blend_count']


@pytest.mark.parametrize('genre',['Jazz','Synth Pop'],ids=['default','edited'])
@pytest.mark.parametrize('legacy',[False,True],ids=['captured','legacy-draft'])
def test_current_family_survives_finalize_reload_and_single_slot_reroll(tmp_path,genre,legacy):
    path = tmp_path/'trip.db'
    store = Store(path)
    try:
        store.init_schema()
        store.accounts.upsert_account('Main','unused.json',None,True)
        store.genres.configure_catalog()
        for video in ('a','b','c'):
            _song(store,video,genre)
        if genre=='Synth Pop':
            store.genres.set_genre_family('synth pop','jazz')
        recipe_id = store.road_trip.save_road_trip_recipe(None,'Trip',100,[],['Jazz'],15,1000)
        state = road_trip.start_draft(store,store.road_trip.get_road_trip_recipe(recipe_id),1000,seed=7)
        road_trip.finish_draft(state,store,1000)
        order = list(state['picks'])
        assert set(order)=={'a','b','c'}
        _assert_blend(state,order,[])
        if legacy:
            state.pop('genre_families')
            for row in state['picked']:
                row['family'] = 'obsolete-family'
        state['revision'] = 'initial'
        store.road_trip.save_road_trip_draft(recipe_id,state,1000)
        store.conn.close()
        store = Store(path)
        store.init_schema()
        state = road_trip.normalized(store.road_trip.get_road_trip_draft(recipe_id),store,1001)
        _assert_blend(state,order,[])
        # Pure normalization also uses the captured current families without a DB.
        _assert_blend(road_trip.normalized(json.loads(json.dumps(state))),order,[])
        _song(store,'replacement','Jazz')
        gone = order[1]
        road_trip.reroll_slot(state,store,1,1002)
        expected = [order[0],'replacement',order[2]]
        _assert_blend(state,expected,[gone])
        state['revision'] = 'rerolled'
        store.road_trip.save_road_trip_draft(recipe_id,state,1002)
        _assert_blend(road_trip.normalized(store.road_trip.get_road_trip_draft(recipe_id),store,1003),
                      expected,[gone])
    finally:
        store.conn.close()


def test_family_change_after_capture_refreshes_labels_without_redrawing(store):
    store.accounts.upsert_account('Main','unused.json',None,True)
    original_family = store.genres.feature_axes(['Synth Pop'])['Synth Pop'][0]
    for video in ('a','b','c'):
        _song(store,video,'Synth Pop')
    recipe_id = store.road_trip.save_road_trip_recipe(None,'Trip',100,[],['Jazz'],15,1000)
    state = road_trip.start_draft(store,store.road_trip.get_road_trip_recipe(recipe_id),1000,seed=7)
    road_trip.finish_draft(state,store,1000)
    assert state['stats']['yours_count']==3
    order = list(state['picks'])
    store.genres.set_genre_family('synth pop','jazz')
    road_trip.normalized(state,store,1001)
    _assert_blend(state,order,[])
    store.genres.set_genre_family('synth pop',original_family)
    road_trip.normalized(state,store,1002)
    assert state['picks']==order and not state['blend_available']
    assert state['stats']['blend_count']==0 and state['stats']['yours_count']==3
    assert state['stats']['mix_targets']['blend']==0


def test_family_edit_immediately_before_reroll_refreshes_both_groups(store):
    store.accounts.upsert_account('Main','unused.json',None,True)
    for video in ('a','b','c'):
        _song(store,video,'Synth Pop')
    recipe_id = store.road_trip.save_road_trip_recipe(None,'Trip',100,[],['Jazz'],15,1000)
    state = road_trip.start_draft(store,store.road_trip.get_road_trip_recipe(recipe_id),1000,seed=7)
    road_trip.finish_draft(state,store,1000)
    assert state['stats']['yours_count']==3 and not state['blend_available']
    order = list(state['picks'])
    _song(store,'replacement','Jazz')
    store.genres.set_genre_family('synth pop','jazz')
    road_trip.reroll_slot(state,store,1,1001)
    _assert_blend(state,[order[0],'replacement',order[2]],[order[1]])
