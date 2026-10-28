import json

import numpy as np
import pytest

from tests.group_support import publish, recordings
from tests.test_group_preparation import seed
from yt_playlist.rec import group_assignment, group_similarity, musical_profiles


def test_changed_recording_gets_a_provisional_generation_tagged_assignment(store):
    tracks = recordings([('House', 20, .4), ('Jazz', 20, .3)])
    seed(store, tracks)
    head = publish(store, tracks)
    key = 'new-song'
    rows = [(key, 1, json.dumps({'library': {'genre': 'House'}, 'discovered': None}), 'digest', True)]
    prepared = group_assignment.prepare(store, rows, [(key, None, musical_profiles.VERSION, None)], 1200)
    assert len(prepared) == 1
    version = prepared[0][2]
    assert version and next(group for group in head['groups'] if group['version'] == version)['name'] == 'House'
    store.conn.execute('BEGIN')
    with store.conn:
        store.taste_groups.publish_assignments(prepared)
    assert store.taste_groups.assignment(key)['version'] == version
    # Published historical membership stays exact; the hint is a separate provisional projection.
    assert all(key not in group['members'] for group in store.taste_groups.head(members=True)['groups'])
    publish(store, tracks, 1300, head)
    assert store.taste_groups.assignment(key) is None


def test_unknown_recording_and_budget_deferral_do_not_fabricate_membership(store):
    tracks = recordings([('House', 20, .4)])
    seed(store, tracks)
    publish(store, tracks)
    rows = [('unknown', 1, json.dumps({'library': {'genre': None}, 'discovered': None}), 'digest', True)]
    prepared = group_assignment.prepare(store, rows, [], 1200)
    assert prepared[0][2] is None and prepared[0][4] == 0
    assert group_assignment.prepare(store, rows, [], 1200, seconds=0) == []


def test_tied_child_fits_stay_provisional_unknown_instead_of_choosing_a_random_id(store):
    tracks = recordings([('House', 20, .1), ('House', 20, .95)])
    seed(store, tracks)
    publish(store, tracks)
    rows = [('ambiguous', 1, json.dumps({'library': {'genre': 'House'}, 'discovered': None}), 'digest', True)]
    prepared = group_assignment.prepare(store, rows, [], 1200)
    assert prepared[0][2] is None and prepared[0][3] >= .6


def _individual_assignment_reference(store, head, rows, musical, now):
    """The original per-group calculation, independent of the batched reduction."""
    leaves = [group for group in head['groups'] if not group.get('legacy') and group['leaf']]
    reps = sorted({key for group in leaves for key in group['rep_keys'][:3]})
    anchors = []
    for offset in range(0, len(reps), 32):
        anchors.extend(group_similarity.recording(key, payload['library'], profile)
                       for key, payload, profile in store.taste_groups.inputs_for(reps[offset:offset + 32])
                       if payload['library'] is not None)
    profiles = {key: (json.loads(raw) if raw else None, digest) for key, raw, _version, digest in musical}
    policy = json.loads(head['policy'])
    expected = []
    for key, _revision, raw, _digest, _changed in rows:
        features = json.loads(raw)['library']
        profile, digest = profiles.get(key, (None, None))
        candidate = group_similarity.recording(key, features or {}, profile)
        metric = group_similarity.Similarity([candidate, *anchors], **{
            name: policy[name] for name in ('style_weight', 'audio_cap', 'era_penalty')})
        fits = []
        if features and candidate.known:
            for group in leaves:
                indices = [i + 1 for i, anchor in enumerate(anchors) if anchor.key in group['rep_keys'][:3]]
                scores, support = metric.pairs(0, indices)
                if len(scores) and np.any(support > 0):
                    fits.append((float(np.mean(scores)), float(np.mean(support)), group['version']))
        fits.sort(reverse=True)
        score, support, version = fits[0] if fits else (0., 0., None)
        if (score < policy['assignment_minimum'] or support < policy['assignment_support']
                or len(fits) > 1 and score - fits[1][0] < policy['assignment_margin']):
            version = None
        expected.append((key, head['generation'], version, score, support, digest, now))
    return expected


@pytest.mark.parametrize('shared_representatives', [False, True])
def test_batch_assignment_preserves_exact_individual_scores_and_unknowns(
        store, monkeypatch, shared_representatives):
    tracks = recordings([('House', 20, .15), ('Jazz', 20, .8), ('Folk', 20, .4)])
    seed(store, tracks)
    store.modes.replace_modes([{'mode_id': 90, 'label': 'Legacy', 'families': [],
        'centroid': np.ones(2), 'size': 2, 'rep_keys': [tracks[0].key], 'space': 'content'}], [], 900)
    head = publish(store, tracks)
    compact = store.taste_groups.assignment_head()
    expected_regions = [{'version': group['version'], 'rep_keys': group['rep_keys']}
                        for group in head['groups'] if not group.get('legacy') and group['leaf']]
    assert compact == {'generation': head['generation'], 'policy': head['policy'],
                       'groups': expected_regions}
    assert len(expected_regions) > 1
    if shared_representatives:
        # Controlled overlapping anchors must keep the original tie/margin rules.
        leaves = [group for group in head['groups'] if not group.get('legacy') and group['leaf']]
        leaves[1]['rep_keys'] = list(leaves[0]['rep_keys'])
        compact['groups'][1]['rep_keys'] = list(leaves[0]['rep_keys'])
        monkeypatch.setattr(store.taste_groups, 'assignment_head', lambda: compact)
    features = [{'genre': 'House', 'year': '1980'}, {'genre': 'Jazz', 'year': '2000'},
                {'genre': 'Folk'}, {'genre': 'unrecognized'}, {}, None]
    rows = [(f'candidate-{i}', 1, json.dumps({'library': value, 'discovered': None}), 'input', True)
            for i, value in enumerate(features)]
    profile = musical_profiles.resolve([], overrides={'mood_relaxed': .7, 'mood_aggressive': .3,
        'instrumental': .2, 'danceability': .6, 'bpm': 110})
    musical = [(row[0], json.dumps(profile), musical_profiles.VERSION, f'profile-{i}')
               for i, row in enumerate(rows)]
    expected = _individual_assignment_reference(store, head, rows, musical, 1200)
    original, calls = group_similarity.Similarity.pairs, []

    def pairs(metric, row, others):
        calls.append(len(others))
        return original(metric, row, others)

    def no_full_head(*args, **kwargs):
        pytest.fail('Provisional assignment loaded the full graph or definitions')

    monkeypatch.setattr(group_similarity.Similarity, 'pairs', pairs)
    monkeypatch.setattr(store.taste_groups, 'head', no_full_head)
    monkeypatch.setattr(store.taste_groups, 'definitions', no_full_head)
    assert group_assignment.prepare(store, rows, musical, 1200) == expected
    assert 0 < len(calls) <= len(rows), 'A candidate/anchor pair was recalculated per group'


def test_compact_assignment_read_still_rejects_a_new_generation(store):
    tracks = recordings([('House', 20, .4)])
    seed(store, tracks)
    head = publish(store, tracks)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.taste_groups.assignment_head()['generation'] == head['generation']
        publish(store, tracks, 1300, head)
        assert not snapshot.publish(lambda: pytest.fail('Published assignments against an old head'))


def test_many_partial_regions_preserve_exact_scores_with_bounded_reductions(store, monkeypatch):
    tracks = recordings([('House', 20, .15), ('Jazz', 20, .8)])
    seed(store, tracks)
    head = publish(store, tracks)
    keys = [track.key for track in tracks[:3]]
    representatives = [[], ['missing'], [keys[0]], [keys[0], keys[0]],
                       [keys[2], keys[0]], [keys[2], keys[1], keys[0]],
                       [keys[0], 'missing', keys[2]]]
    # Exercise the larger graph shape without changing the real fit policy or
    # replacing pair scoring. Missing and duplicate keys are deliberate fixtures.
    head['groups'] = [{'version': f'fixture-{i}', 'leaf': True, 'legacy': False,
                       'rep_keys': representatives[i % len(representatives)]}
                      for i in range(180)]
    compact = {**head, 'groups': [{'version': group['version'], 'rep_keys': group['rep_keys']}
                                for group in head['groups']]}
    monkeypatch.setattr(store.taste_groups, 'assignment_head', lambda: compact)
    features = [{'genre': 'House', 'year': '1980'}, {'genre': 'Jazz'},
                {'genre': 'unrecognized'}, None] * 8
    rows = [(f'candidate-{i}', 1, json.dumps({'library': value}), 'input', True)
            for i, value in enumerate(features)]
    profile = musical_profiles.resolve([], overrides={'mood_relaxed': .7, 'mood_aggressive': .3,
        'instrumental': .2, 'danceability': .6, 'bpm': 110})
    musical = [(row[0], json.dumps(profile), musical_profiles.VERSION, f'profile-{i}')
               for i, row in enumerate(rows)]
    expected = _individual_assignment_reference(store, head, rows, musical, 1200)
    original_mean, reductions = np.mean, []

    def mean(values, *args, **kwargs):
        reductions.append(values.shape)
        return original_mean(values, *args, **kwargs)

    monkeypatch.setattr(group_assignment.np, 'mean', mean)
    assert group_assignment.prepare(store, rows, musical, 1200) == expected
    assert reductions
    # At most two reductions for each of the three representative counts per
    # recording, rather than two Python/NumPy calls for each of 180 regions.
    assert len(reductions) <= 6 * len(rows)
