"""Current family edits through real recipe, draft, restart and reroll APIs."""
import json

import pytest

from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import file_consumer_store as store
from yt_playlist.core.store import Store
from yt_playlist.rec import road_trip


def _song(store, video, genre):
    seed_publication_subjects(store, [{'key': f'song {video}|artist {video}', 'video_id': video,
        'title': 'Song ' + video, 'artist': 'Artist ' + video, 'duration_s': 300,
        'genre': genre, 'year': 2000}])


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
def test_current_family_survives_finalize_reload_and_single_slot_reroll(tmp_path,genre,legacy,consumer_store):
    path = tmp_path/'trip.db'
    store = consumer_store(path)
    try:
        store.accounts.upsert_account('Main','unused.json',None,True)
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


@pytest.mark.parametrize('size', [20, 6000])
def test_draft_start_reads_the_full_library_once_and_batches_family_queries(store, monkeypatch, size):
    store.accounts.upsert_account('Main', 'unused.json', None, True)
    store.genres.set_genre_family('synth pop', 'jazz')
    songs = [{'key': f'song {i}|artist {i}', 'video_id': f'v{i}', 'title': f'Song {i}',
              'artist': f'Artist {i}', 'album': '', 'thumbnail': None, 'duration': 300,
              'genre': 'Synth Pop' if i % 2 else 'Jazz', 'year': 2000, 'liked': False}
             for i in range(size)]
    reads = []
    def library():
        reads.append(len(songs))
        return songs
    monkeypatch.setattr(store.rec_query, 'library_songs', library)
    recipe_id = store.road_trip.save_road_trip_recipe(None, 'Trip', 100, [], ['Jazz'], 15, 1000)
    recipe = store.road_trip.get_road_trip_recipe(recipe_id)
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        state = road_trip.start_draft(store, recipe, 1000, seed=7)
    finally:
        store.conn.set_trace_callback(None)
    assert reads == [size]
    meanings = [sql for sql in statements if 'genre_catalog' in sql or 'genre_alias' in sql]
    assert len(meanings) <= 2, len(meanings)
    assert state['blend_available'] and state['stats']['blend_count'] == 1
    assert all(row['family'] == 'jazz' for row in state['picked'])
    assert state['own_facts_left'] == 0 and state['building']  # passenger input remains
    # Re-picking keeps the seed and interpretation, but reacquires the live full library.
    initial = list(state['picks'])
    road_trip.repick(state, store, 1001)
    assert state['picks'] == initial and reads == [size, size]
