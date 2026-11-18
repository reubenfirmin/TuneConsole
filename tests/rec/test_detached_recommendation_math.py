"""Recommendation rules over detached facts, without schema or model preparation."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
import json

import numpy as np
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.rec import (candidate_policy, candidate_refresh, content_model, eval_recs,
    graduation, layers, model_refresh, musical_preparation, negative, ppr, product_comparison,
    scoring, taste_analysis, transient)

pytestmark = pytest.mark.usefixtures('no_database')


def test_centroids_skip_empty_and_opposing_members_and_default_to_uniform_weights():
    matrix = np.array([[2., 0.], [-2., 0.], [0., 4.]])
    assert scoring._centroid_from_rows(matrix, []) is None
    assert scoring._centroid_from_rows(matrix, [0, 1]) is None
    taste = scoring._centroids_from_playlist_rows(matrix,
        [('cancelled', 1, [0, 1]), ('east', 2, [0]), ('north', 3, [2])], {})
    assert taste.titles == ['east', 'north'] and taste.pids == [2, 3]
    np.testing.assert_allclose(taste.centroids, np.eye(2))
    np.testing.assert_allclose(taste.weights, [.5, .5])


def test_dislike_direction_is_bounded_zero_safe_and_opposing_evidence_cancels():
    candidates = np.array([[2., 0.], [-3., 0.], [0., 1.], [0., 0.]])
    np.testing.assert_allclose(negative._penalty_from_rows(candidates, candidates, [0], .4), [-.4, 0, 0, 0])
    np.testing.assert_allclose(negative._penalty_from_rows(candidates, candidates, [0, 1], .4), 0)
    np.testing.assert_allclose(negative._penalty_from_rows(candidates, candidates, [3], .4), 0)


def test_axis_weights_preserve_mutes_and_unknown_metadata_neutrality():
    inputs = dict(keys=['jazz', 'rock', 'unknown'], gw={'jazz': 2., 'rock': 0.}, ew={}, aw={}, pw={},
        leans={}, standing={}, bfac={}, genres={'jazz': 'Jazz', 'rock': 'Rock'}, decades={},
        artists={}, pops={}, pop_min=70, fparams=(1., .5, 2.),
        genre_axes={'Jazz': ('jazz', None), 'Rock': ('rock', None)})
    weights, details = scoring._compute_axis_weights(inputs, with_details=True)
    assert weights == {'jazz': 1., 'rock': 0., 'unknown': 1.}
    assert details['unknown']['metadata_present']['genre'] is False
    assert details['rock']['raw_product'] == 0


def test_fingerprint_hides_facets_and_promotes_pinned_subgenres_without_duplicates():
    inputs = dict(bd={'families': {'jazz': .7, 'rock': .3}, 'breadth': .6},
        w={'genre:jazz': 1.5}, leans={'genre:bebop': 1.2}, hidden={'genre:rock'},
        tleans={'genre:jazz': .2}, fgain=1., fmin=.5, fmax=2.,
        family_members={'jazz': ['jazz', 'bebop', 'swing']}, era_shares=[('1960', 1.)],
        bias=0., tuning_remaining=60)
    result = taste_analysis._compute_fingerprint(inputs)
    assert [row['name'] for row in result['families']] == ['jazz', 'bebop']
    assert [row['name'] for row in result['families'][0]['subgenres']] == ['swing']
    assert result['families'][0]['live'] > result['families'][0]['effective']
    assert result['families'][1]['pinned'] and result['has_steering']
    assert result['tuning_remaining'] == 60


def test_now_and_session_use_explicit_gates_windows_and_decay():
    now = 200000.
    evidence = layers.RecentEvidence({'fresh': now, 'old': now - 4 * 3600, 'outside': now - 25 * 3600},
        [{'mode_id': 1, 'centroid': np.array([1., 0.])},
         {'mode_id': 2, 'centroid': np.array([0., 1.])}], np.eye(2), {'fresh': 0, 'old': 1})
    assert layers.now_mode_mix(evidence, now, window_h=1, min_events=2)[:2] == (None, 0)
    assert layers.now_mode_mix(evidence, now, window_h=6, min_events=2)[:2] == ({1: .5, 2: .5}, 2)
    shares, count, _ = layers.session_mode_mix(evidence, now, min_events=2, halflife_h=4)
    assert count == 2 and shares == pytest.approx({1: 2 / 3, 2: 1 / 3})
    np.testing.assert_allclose(layers.session_tilt(evidence.latest, now, np.eye(2), evidence.index,
        min_events=2, halflife_h=4), [.5, .25])
    coverage = layers.evidence_coverage(evidence, now, window_h=1)
    assert coverage['now']['total'] == 1 and coverage['session']['total'] == 2


def test_transient_decay_preserves_unknown_denominator_and_negative_artist_safety():
    now = 86400.
    facets = transient.facets_from_metadata(['a', 'b', 'unknown'], {'a': 'Jazz'},
        {'b': '1990'}, {'a': 'artist'}, {'Jazz': ('jazz', 'bebop')})
    contributions = transient.signed_contributions(now, [(0, 2., ['a', 'a', 'unknown'])],
        [], [], [('a', now)], [('b', now)], play_w=1., like_w=1., dislike_w=.25, skip_w=.5,
        play_hl=1., mood_hl=1., like_hl=1., dislike_hl=1., skip_hl=1.)
    leans = transient.facet_leans_from_inputs(contributions, facets, [({'jazz'}, .4), ({'jazz'}, .2)])
    assert leans == pytest.approx({'genre:jazz': .65, 'genre:bebop': .25,
                                 'artist:artist': .5, 'era:1990': -.5})
    assert transient.play_leans_from_inputs([('a', 0), ('unknown', now)], facets, now,
        weight=.8, half_life_d=1) == pytest.approx({'genre:jazz': .4, 'genre:bebop': .4, 'artist:artist': .4})
    matrix = np.array([[2., 0.], [0., 0.]])
    tilt = transient.tilt_from_inputs(now, matrix, {'a': 0, 'zero': 1}, [],
        [('a', 0), ('zero', now), ('missing', now)], [], [], mood_hl=1.,
        play_hl=1., play_w=.8, like_hl=1., like_w=1., skip_hl=1., skip_w=1.)
    np.testing.assert_allclose(tilt, [.5, 0.])


def test_detached_walk_conserves_mass_including_isolated_tracks_and_rejects_stale_modes():
    graph = ppr.transition_from_baskets([['a', 'b'], ['isolated']])
    _, transition, index = graph
    ranks = ppr.ppr_rank(transition, [index['isolated']], iters=100)
    assert ranks.sum() == pytest.approx(1.)
    assert ranks[index['isolated']] == pytest.approx((1 - .85) / (1 - .85 / 3))
    assert ranks[index['a']] == pytest.approx(ranks[index['b']])
    modes = [{'mode_id': 7, 'centroid': np.ones(3)}]
    assert ppr.rankings_from_inputs(modes, graph, (['a', 'b'], np.eye(2), {'a': 0, 'b': 1}),
        alpha=.85, iters=10, tol=0., depth=3) == {}


def test_mood_components_keep_missing_audio_neutral_and_do_not_renormalize_decay():
    matrix = np.array([[2., 0.], [0., 0.]])
    result = scoring.mood_components_from_inputs([.2, .4], matrix, np.array([-.1, 0.]),
        np.array([.5, 0.]), None, session_alpha=.8, audio_boost=None, audio_weight=1.)
    np.testing.assert_allclose(result['combined'], [.2 - .1 + scoring.MOOD_ALPHA * .5, .4])
    assert scoring.discovery_multiplier(0., 2., 2.) is None
    assert graduation.mood_contributions(['a', 'a', 'unknown'], {'genre:jazz': ['a']}, 2.) == {'genre:jazz': 1.}
    assert graduation.mood_contributions(['a'], {'genre:jazz': ['a']}, -2.) == {}
    assert graduation.migrated_lean(1.5, 1., 1.1) * 1.1 == pytest.approx(1.5)


def test_freshness_tracks_decay_configuration_and_deadlines_without_source_writes():
    inputs = dict(choices={}, bandit={}, choice_decay=False, album_feedback={}, models={}, home={}, exposure={})
    ranking = {'records': {}, 'params': {'comfort_min_plays': 4}}
    neutral = candidate_refresh.preference_fingerprint(ranking, now=0, **inputs)
    assert candidate_refresh.preference_fingerprint(ranking, now=3600, **inputs) == neutral
    muted = {'records': {'genre:jazz': {'weight': 0., 'updated_at': 0}}, 'params': ranking['params']}
    assert candidate_refresh.preference_fingerprint(muted, now=0, **inputs) == candidate_refresh.preference_fingerprint(muted, now=3600, **inputs)
    decaying = deepcopy(muted)
    decaying['records']['genre:jazz']['weight'] = 1.5
    assert candidate_refresh.preference_fingerprint(decaying, now=0, **inputs) != candidate_refresh.preference_fingerprint(decaying, now=3600, **inputs)
    assert not candidate_refresh.refresh_due('100', neutral, neutral, 99)
    assert candidate_refresh.refresh_due('100', neutral, neutral, 100)
    assert candidate_refresh.refresh_due('', neutral, 'changed', 0)


def preparation_inputs():
    content = content_model.ContentInputs(['a', 'b'], {'a': ('Jazz', 1960), 'b': ('Rock', 1990)}, {})
    return model_refresh.PreparationInputs(method='svd', dim=2, baskets=[['a', 'b']], weights=None,
        fingerprints=dict.fromkeys(('rec_fit_inputs', 'rec_artist_fit_inputs', 'rec_mode_fit_inputs')),
        content=content, previous={'model': None, 'inputs': None, 'revision': '', 'library': [], 'discovered': []},
        discoveries=[], axes={'Jazz': ('jazz', None), 'Rock': ('rock', None)},
        artist_profiles={}, artist_baskets=[], mode_params={'modes_k': 2, 'modes_min_members': 2,
        'modes_match_threshold': .5}, modes=[], next_mode_id=1,
        genres={'a': 'Jazz', 'b': 'Rock'}, track_count=0)


def test_model_fingerprints_chain_through_new_content_and_reuse_unchanged_fits(monkeypatch):
    from yt_playlist.rec import mode_discovery
    original = mode_discovery.discover_from_inputs
    discover = Mock(wraps=original)
    monkeypatch.setattr(mode_discovery, 'discover_from_inputs', discover)
    captured = preparation_inputs()
    first = model_refresh.calculate(captured, 100, new_generation=lambda: 'first')
    reference = {'model': first['settings']['rec_content_model'], 'inputs': first['settings']['rec_content_inputs'],
        'revision': first['settings']['rec_content_genre_revision'], 'library': first['vectors']['rec_content_vectors'],
        'discovered': first['vectors']['rec_discovered_content_vectors'], 'generation': 'first'}
    fitted = replace(captured, previous=reference, fingerprints=first['settings'])
    assert model_refresh.calculate(fitted, 101)['vectors'] == {}
    discover.assert_called_once()
    modified = replace(fitted, content=content_model.ContentInputs(['a', 'b'],
        {'a': ('Jazz', 1960), 'b': ('Jazz', 1990)}, {}), genres={'a': 'Jazz', 'b': 'Jazz'})
    changed = model_refresh.calculate(modified, 102, new_generation=lambda: 'changed')
    assert 'rec_vectors' not in changed['vectors']
    assert changed['settings']['rec_mode_fit_inputs'] != first['settings']['rec_mode_fit_inputs']
    assert discover.call_count == 2
    keys, matrix = discover.call_args.args
    actual = dict(changed['vectors']['rec_content_vectors'])
    for index, key in enumerate(keys):
        np.testing.assert_array_equal(matrix[index], np.frombuffer(actual[key], dtype=np.float32))
    model = json.loads(changed['settings']['rec_content_model'])
    assert discover.call_args.kwargs['space'] == content_model.content_model_fingerprint(model)


def test_detached_budget_rejection_precedes_numerical_fitting(monkeypatch):
    from yt_playlist.rec import embedding_training
    fit = Mock(side_effect=AssertionError('unadmitted fit'))
    monkeypatch.setattr(embedding_training, 'build_vectors_from_baskets', fit)
    with pytest.raises(model_refresh.MemoryBudgetExceeded):
        model_refresh.calculate(preparation_inputs(), 100, memory_bytes=1)
    fit.assert_not_called()


def test_musical_profiles_require_admitted_inputs_and_keep_unknown_explicit():
    rows = [('missing', 0, json.dumps({'library': None, 'discovered': None}), None, True)]
    assert musical_preparation.recordings(rows, inputs={})[0][1] is None
    with pytest.raises(TypeError):
        musical_preparation.recordings(rows)
    with pytest.raises(TypeError):
        musical_preparation.masked_recordings(rows)


def test_assignment_is_stable_per_day_and_respects_epoch_constraints():
    current = dict(phase='observing', started_at=0, exposure_until=2 * 86400, constraints='intent',
        seed='fixed', run_id=7, policy={'version': 'v1'}, generation='generation')
    first = product_comparison.select_assignment(current, 'mixes', 'one', 100, paused=False, constraints='intent')
    second = product_comparison.select_assignment(current, 'albums', 'two', 200, paused=False, constraints='intent')
    assert first['arm'] == second['arm'] and first['unit'] == second['unit']
    for now, paused, constraints in ((100, True, 'intent'), (100, False, 'changed'), (2 * 86400, False, 'intent')):
        assert product_comparison.select_assignment(current, 'radio', 'three', now,
                                                    paused=paused, constraints=constraints) is None


def test_detached_evaluation_retains_missing_and_causal_boundaries(monkeypatch):
    from yt_playlist.rec import embedding_training
    keys = ['before', 'organic', 'generated']
    monkeypatch.setattr(embedding_training, 'build_vectors_from_baskets',
                        lambda *args, **kw: (keys, np.array([[1., 0.], [1., .1], [0., 1.]])))
    result = eval_recs.causal_recall(baskets=[keys], coverage={'source': 'captured'}, weights=None,
        before={'before'}, after={'organic', 'generated'}, generated={'generated'},
        cutoff=100, end=200, k=1, dim=2, method='svd')
    assert result['trials'] == result['hits'] == result['generated_excluded'] == 1
    assert result['recall'] == 1.
    assert eval_recs.holdout_window((None, None)) == 1
    assert eval_recs.projection_from_inputs(((keys, None, {}), {}, {}))['reason'] == 'No reference embedding'


def test_candidate_selection_handoff_passes_identity_groups_policy_and_revision():
    context = candidate_policy.SelectionInputs(memberships={'cover': frozenset({'work'})},
        destination_exclusions=frozenset({'saved'}), playlist_policy={'allow_multiple_versions': True},
        revision='groups-and-policy:7')
    policy = Mock(return_value=['cover'])
    assert candidate_policy.select_candidates(['saved', 'cover'], context, policy=policy) == ['cover']
    policy.assert_called_once_with(['saved', 'cover'], context)
