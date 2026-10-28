from dataclasses import replace

import pytest

from tests.group_support import recordings
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.rec import group_discovery as discovery, group_similarity as similarity, group_names, musical_profiles


def test_small_pop_album_survives_metal_library_without_a_membership_floor():
    tracks = recordings([('Death Metal', 180, .9), ('Thrash Metal', 60, .8), ('Pop', 12, .25)])
    result = discovery.discover(tracks)
    leaves = [node for node in result['nodes'] if node['leaf']]
    pop = {track.key for track in tracks if track.genre == 'Pop'}
    assert any(set(node['members']) == pop for node in leaves)
    assert set().union(*(set(node['members']) for node in leaves)) == {track.key for track in tracks}
    for parent in [node for node in result['nodes'] if not node['leaf']]:
        children = [node for node in result['nodes'] if node['parent'] == parent['index']]
        assert set(parent['members']) == set().union(*(set(child['members']) for child in children))


def test_count_follows_musical_diversity_and_has_no_minimum_library_size():
    narrow = discovery.discover(recordings([('Death Metal', 90, .8)]))
    eclectic = discovery.discover(recordings([('Death Metal', 20, .8), ('Pop', 20, .2), ('Jazz', 20, .4)]))
    album = discovery.discover(recordings([('Pop', 8, .2)]))
    assert discovery.summary(narrow)['leaves'] < discovery.summary(eclectic)['leaves']
    assert discovery.summary(album)['assigned'] == 8


def test_missingness_and_genre_priors_do_not_manufacture_audio_similarity():
    tracks = [similarity.recording(str(i), {'genre': None}, musical_profiles.resolve([])) for i in range(8)]
    result = discovery.discover(tracks)
    assert result['nodes'] == [] and len(result['unknown']) == 8
    prior = similarity.recording('prior', {'genre': 'Death Metal'}, musical_profiles.resolve([], genre='Death Metal'))
    assert not any(prior.support)
    assert prior.known
    assert not similarity.recording('junk', {'genre': 'some unrecognized provider value'}).known
    mixed = {'version': musical_profiles.VERSION, 'dimensions': {'groove': {
        'value': .8, 'support': .5, 'evidence': [{'kind': 'inherited'}, {'kind': 'reported'}]}}}
    assert not any(similarity.recording('mixed', {'genre': 'House'}, mixed).support)


def test_tempo_half_time_is_not_a_large_musical_difference():
    tracks = recordings([('House', 1, .3)])
    index = similarity.DIMENSIONS.index('tempo')
    a = list(tracks[0].values)
    b = list(a)
    a[index], b[index] = (70 - 30) / 270, (140 - 30) / 270
    metric = similarity.Similarity([replace(tracks[0], values=tuple(a)), replace(tracks[0], values=tuple(b))])
    score, support = metric.pairs(0, [1])
    assert score[0] > .99 and support[0] > 0


def test_era_does_not_split_one_supported_musical_style():
    tracks = recordings([('House', 80, .6)])
    result = discovery.discover(tracks)
    assert discovery.summary(result)['leaves'] == 1


def test_provider_coverage_and_duplicate_sound_do_not_create_extra_regions():
    tracks = recordings([('House', 48, .7)])
    sparse = [replace(track, support=(0.,) * len(similarity.DIMENSIONS)) if i % 2 else track
              for i, track in enumerate(tracks)]
    assert discovery.summary(discovery.discover(sparse))['leaves'] == 1
    duplicated = tracks + [replace(track, key=f'duplicate:{track.key}') for track in tracks]
    assert discovery.summary(discovery.discover(duplicated))['leaves'] == 1


def test_weak_styleless_audio_cannot_bridge_unrelated_supported_interests():
    tracks = recordings([('House', 25, .4), ('Jazz', 25, .4)])
    bridge = replace(tracks[0], key='bridge', genre='', family='', support=(.35,) * len(similarity.DIMENSIONS))
    result = discovery.discover([*tracks, bridge])
    assert 'bridge' in result['unknown']
    assert len([node for node in result['nodes'] if node['leaf']]) == 2
    # Strong independent sound evidence can still represent an untagged interest.
    strong = replace(bridge, key='strong', support=(1.,) * len(similarity.DIMENSIONS))
    assert strong.known


def test_names_contrast_supported_siblings_without_numeric_suffixes():
    tracks = recordings([('House', 35, .1), ('House', 35, .95)])
    result = discovery.discover(tracks)
    names = group_names.name_groups(result, tracks)
    leaves = [names[node['index']] for node in result['nodes'] if node['leaf']]
    assert len(leaves) == 2
    assert len({item['name'] for item in leaves}) == 2
    assert all(item['display'] for item in leaves)
    assert any('Instrumental' in item['name'] for item in leaves)


def test_unsupported_name_collision_hides_finer_groups():
    tracks = recordings([('House', 8, .5)])
    result = {'nodes': [{'index': 0, 'parent': None, 'leaf': False, 'rows': list(range(8))},
                        {'index': 1, 'parent': 0, 'leaf': True, 'rows': list(range(4))},
                        {'index': 2, 'parent': 0, 'leaf': True, 'rows': list(range(4, 8))}]}
    names = group_names.name_groups(result, tracks)
    assert not names[1]['display'] and not names[2]['display']


def test_supported_names_survive_small_updates_but_release_unsupported_traits():
    tracks = recordings([('House', 12, .1), ('House', 12, .95)])
    result = discovery.discover(tracks)
    for node in result['nodes']:
        node['group_id'] = str(node['index'])
    first = group_names.name_groups(result, tracks)
    previous = [dict(first[node['index']], group_id=node['group_id']) for node in result['nodes']]
    dimension = similarity.DIMENSIONS.index('instrumental')

    def updated(low, high):
        changed = []
        for track in tracks:
            values = list(track.values)
            values[dimension] = low if values[dimension] < .5 else high
            changed.append(replace(track, values=tuple(values)))
        return changed

    second = group_names.name_groups(result, updated(.38, .62), previous=previous)
    assert [item['name'] for item in second.values()] == [item['name'] for item in first.values()]
    third = group_names.name_groups(result, updated(.5, .5), previous=previous)
    assert all(not item['name'].startswith(('Instrumental ', 'Vocal ')) for item in third.values())


def test_sparse_work_is_bounded_and_cancellable():
    tracks = recordings([('House', 200, .5)])
    result = discovery.discover(tracks)
    assert len(result['edges']) <= len(tracks) * discovery.Policy().neighbors
    with pytest.raises(WorkBudgetExceeded):
        with work_budget(0):
            discovery.discover(tracks)
    with pytest.raises(WorkBudgetExceeded, match='group work ceiling'):
        discovery.discover(recordings([('House', 10, .5), ('Jazz', 10, .5)]), policy=discovery.Policy(max_groups=1))


def test_recursive_baseline_and_graph_measure_the_same_supported_minority():
    tracks = recordings([('Death Metal', 80, .9), ('Pop', 12, .2)])
    results = [discovery.discover(tracks, policy=discovery.Policy(engine=engine)) for engine in ('graph', 'recursive')]
    assert all(discovery.summary(result)['assigned'] == len(tracks) for result in results)
    assert all(any(len(n['members']) == 12 for n in result['nodes'] if n['leaf']) for result in results)


def test_adjusted_rand_handles_sparse_label_products():
    assert discovery.agreement([0, 0, 2, 2], [1, 1, 0, 0]) == 1
    assert discovery.agreement([0, 0, 0], [0, 1, 2]) == 0


def test_three_tracks_cut_a_weak_connection_and_rejoin_when_it_strengthens():
    tracks = recordings([('House', 3, .1)])
    # There is no genre label to stand in for internal cohesion. Independent
    # sound evidence supports each recording, including an isolated singleton.
    tracks = [replace(track, genre='', family='') for track in tracks]
    weak = replace(tracks[-1], values=(.95,) * len(tracks[-1].values))
    policy = discovery.Policy(split_gain=1, agreement=1)
    separated = discovery.discover([*tracks[:2], weak], policy=policy)
    assert sorted(len(n['members']) for n in separated['nodes'] if n['leaf']) == [1, 2]
    assert separated['unknown'] == []
    joined = discovery.discover(tracks, policy=policy)
    assert [len(n['members']) for n in joined['nodes'] if n['leaf']] == [3]


def test_supported_singletons_do_not_need_cross_region_support_to_separate():
    tracks = recordings([('House', 2, .1), ('Jazz', 1, .95)])
    tracks = [replace(track, support=(0.,) * len(track.support)) for track in tracks]
    result = discovery.discover(tracks)
    leaves = [set(node['members']) for node in result['nodes'] if node['leaf']]
    assert {tracks[-1].key} in leaves
    assert {tracks[0].key, tracks[1].key} in leaves
