import json
from dataclasses import replace

import pytest

from tests.group_support import recordings
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import group_discovery, group_preparation, musical_profiles


def seed(store, tracks):
    with store.conn:
        for i, track in enumerate(tracks):
            library = {'genre': track.genre, 'year': str(track.year or ''), 'audio': {}}
            payload = json.dumps({'library': library, 'discovered': None})
            store.conn.execute('INSERT OR REPLACE INTO rec_prepared_content VALUES (?,?,?,?,?)',
                               (track.key, payload, str(i), 1, i + 1))
            profile = {'schema': 1, 'version': musical_profiles.VERSION, 'dimensions': {
                name: {'value': value, 'support': support, 'evidence': [{'kind': 'explicit'}]}
                for name, value, support in zip(musical_profiles.DIMENSIONS, track.values, track.support)}}
            store.conn.execute('INSERT OR REPLACE INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                               (track.key, json.dumps(profile), musical_profiles.VERSION, str(i), 1))


def test_background_shadow_is_coalesced_and_does_not_replace_live_modes(store):
    tracks = recordings([('Death Metal', 80, .9), ('Pop', 12, .2)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.due(1000) and worker.step(1000)
    head = store.taste_groups.head(members=True)
    assert head['payload']['shadow'] and head['payload']['serving_policy'] == 'legacy'
    assert not worker.due(1060)
    assert not store.modes.list_modes()
    # Even one change is checked after coalescing; unchanged inputs are cheap acknowledgements.
    with store.conn:
        store.conn.execute('UPDATE rec_musical_profiles SET prepared_at=2 WHERE identity_key=?', (tracks[0].key,))
    assert worker.due(1100)
    # Unchanged musical inputs advance the acknowledgement, not the immutable definition.
    assert not worker.step(1100)
    assert not worker.due(1160)
    assert store.taste_groups.head()['generation'] == head['generation']


def test_stale_and_budget_deferred_work_preserve_complete_last_good_generation(store, monkeypatch):
    tracks = recordings([('Pop', 12, .2)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head()
    original = group_discovery.discover

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        with store.conn:
            store.conn.execute('UPDATE rec_musical_profiles SET prepared_at=prepared_at+1')
        return result

    monkeypatch.setattr(group_discovery, 'discover', race)
    worker.request_rebuild()
    assert not worker.step(1100)
    assert store.taste_groups.head()['generation'] == first['generation']
    monkeypatch.setattr(group_discovery, 'discover', original)
    deferred = group_preparation.Preparation(store, budget=group_preparation.Budget(seconds=0))
    deferred.request_rebuild()
    assert not deferred.step(1200)
    assert store.taste_groups.head()['generation'] == first['generation']


def test_cancellation_propagates_without_publishing_a_partial_generation(store, monkeypatch):
    seed(store, recordings([('Pop', 12, .2)]))
    worker = group_preparation.Preparation(store)
    monkeypatch.setattr(group_discovery, 'discover', lambda *a, **kw: (_ for _ in ()).throw(WorkClosing('stop')))
    with pytest.raises(WorkClosing):
        worker.step(1000)
    assert store.taste_groups.head() is None


def test_new_supported_album_is_represented_on_the_next_update(store):
    metal = recordings([('Death Metal', 60, .9)])
    seed(store, metal)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head(members=True)
    metal_id = first['groups'][0]['group_id']
    pop = recordings([('Pop', 12, .2)])
    seed(store, pop)
    assert worker.step(1100)
    second = store.taste_groups.head(members=True)
    assert any(group['group_id'] == metal_id and set(group['members']) == {r.key for r in metal}
               for group in second['groups'])
    assert any(set(group['members']) == {r.key for r in pop} for group in second['groups'])


@pytest.mark.parametrize('target_genre', ['Death Metal', 'House'])
def test_corrected_genre_or_profile_moves_on_the_next_update(store, target_genre):
    tracks = recordings([('House', 30, .1), (target_genre, 30, .95)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    original = store.taste_groups.head(members=True)
    leaves = [g for g in original['groups'] if g['leaf']]
    assert len(leaves) == 2
    target = next(g for g in leaves if tracks[-1].key in g['members'])
    changed = replace(tracks[-1], key=tracks[0].key)
    seed(store, [changed])
    assert worker.due(1900) and worker.step(1900)
    current = store.taste_groups.head(members=True)
    actual = next(g for g in current['groups'] if g['leaf'] and changed.key in g['members'])
    assert actual['group_id'] == target['group_id']
    assert set(actual['members']) == {r.key for r in tracks[30:]} | {changed.key}
    assert {g['group_id'] for g in current['groups']} == {g['group_id'] for g in original['groups']}
    assert current['payload']['last_structure_at'] == original['payload']['last_structure_at']
    historical = store.taste_groups.definitions(original['generation'], members=True)
    assert historical == original['groups']


def test_member_that_loses_all_support_becomes_unknown_without_stale_assignment(store):
    tracks = recordings([('House', 30, .1), ('House', 30, .95)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    original = store.taste_groups.head(members=True)
    changed = replace(tracks[0], genre='', values=(0.,) * len(tracks[0].values),
                      support=(0.,) * len(tracks[0].support))
    seed(store, [changed])
    assert worker.step(1900)
    current = store.taste_groups.head(members=True)
    assert all(changed.key not in g['members'] for g in current['groups'])
    assert store.conn.execute('SELECT 1 FROM rec_group_unknown WHERE generation=? AND identity_key=?',
                              (current['generation'], changed.key)).fetchone()
    assert store.taste_groups.definitions(original['generation'], members=True) == original['groups']


def test_initial_three_track_graph_splits_and_rejoins_while_backlog_continues(store):
    tracks = recordings([('House', 3, .1)])
    seed(store, tracks)
    with store.conn:
        store.conn.execute("INSERT INTO rec_content_pending(identity_key) VALUES ('not-captured-yet')")
        store.conn.execute("INSERT INTO rec_musical_sources VALUES ('artist-source','test','artist','artist','{}',1)")
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head(members=True)
    assert [g['size'] for g in first['groups'] if g['leaf']] == [3]
    changed = replace(tracks[-1], values=(.95,) * len(tracks[-1].values))
    seed(store, [changed])
    assert not worker.due(1030)  # Bounded coalescing, never a structural freeze.
    assert worker.due(1060) and worker.step(1060)
    second = store.taste_groups.head(members=True)
    assert sorted(g['size'] for g in second['groups'] if g['leaf']) == [1, 2]
    seed(store, [tracks[-1]])
    assert worker.step(1120)
    third = store.taste_groups.head(members=True)
    assert [g['size'] for g in third['groups'] if g['leaf']] == [3]
    assert third['groups'][0]['group_id'] == first['groups'][0]['group_id']
    assert store.prepared_content.has_pending() and store.musical_profiles.pending()
    assert store.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert store.taste_groups.definitions(second['generation'], members=True) == second['groups']


def test_fresh_library_publishes_from_real_capture_before_either_queue_drains(store):
    from tests.conftest import seed_catalog_track
    from yt_playlist.rec import group_serving, progressive

    for i in range(12):
        seed_catalog_track(store, f'fresh {i}|artist {i}', f'Fresh {i}', f'Artist {i}',
                           video_id=f'fresh{i}', genre=('House', 'House', 'Jazz')[i % 3])
    with store.conn:
        store.conn.executemany('INSERT INTO rec_musical_sources VALUES (?,?,?,?,?,1)',
            [(f'source{i}', 'test', 'artist', f'artist{i}', '{}') for i in range(12)])
    preparation = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    assert preparation.capture(1000)  # First cached source batch.
    assert preparation.capture(1001)  # First three recordings, without a full-corpus fit.
    assert preparation.groups.step(1002)
    assert group_serving.prepare(store, 1003)
    first = group_serving.view(store)
    assert first['counts']['known'] == 3
    assert first['groups'] and not store.modes.list_modes()
    assert not any(g['mix_ready'] for g in first['groups'])
    progress = group_serving.progress(store)
    assert progress['prepared'] == 3 and progress['pending_recordings'] == 9
    assert progress['sources'] == {'artist': 11}
    # More inputs improve the initial graph while both queues remain nonempty.
    assert preparation.capture(1060) and preparation.capture(1061)
    assert preparation.groups.step(1062)
    assert group_serving.prepare(store, 1063)
    assert group_serving.view(store)['counts']['known'] == 6
    assert store.prepared_content.has_pending() and store.musical_profiles.pending()
