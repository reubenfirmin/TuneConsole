"""Independent response laws and holdout boundaries for metadata pooling."""
import json
import numpy as np
import pytest

from yt_playlist.rec import vibe_joint_model as joint
from experiments.audio_lab_tools import vibe_pooling_metadata as metadata
from experiments.audio_lab_tools import vibe_pooling_descriptors as descriptors
from experiments.audio_lab_tools import vibe_pooling_transfer as transfer
from experiments.audio_lab_tools import vibe_pooling_pairs as pairs
from experiments.audio_lab_tools import vibe_pooling_source_assignment as source_assignment
from experiments.audio_lab_tools import vibe_pooling_ranking as ranking
from experiments.audio_lab_tools import vibe_pooling_selection as selection
from experiments.audio_lab_tools import vibe_pooling_offset as offset
from experiments.audio_lab_tools import vibe_pooling_source_genre as source_genre
from experiments.audio_lab_tools import vibe_pooling_derivatives as derivatives


def test_recovers_metadata_gradient_for_wholly_unseen_artist():
    rows = []
    for artist in range(1, 5):
        for genre in ('ambient', 'techno'):
            for source in ('provider_measurement/control', 'audio_measurement/control'):
                for prior in (.15, .325, .5, .675, .85):
                    # Fully crossed metadata, artist, genre and source factors.
                    # The response law supplies the expected transfer gradient,
                    # independently of the fitted model's implementation.
                    score = .15 + .5 * prior + (artist - 2.5) * .015 \
                        + .03 * (genre == 'ambient') + .04 * source.startswith('provider')
                    rows.append({'recording': len(rows) + 1, 'measurement': len(rows) + 1,
                        'artists': [artist], 'genre': genre, 'album': None, 'source': source,
                        'score': score, 'support': .8, 'metadata_prior': prior})
    original_design, original_terms, original_factor = joint._design, joint.terms, joint._factor
    with metadata.design_context():
        value = metadata.fit(rows, 'relax')
        posterior = joint.Posterior(value)
        predictions = []
        for prior in (.15, .85):
            score, interval = metadata.predict(posterior, {'recording': 1001, 'artists': [999],
                'genre': 'ambient', 'album': None, 'metadata_prior': prior})
            assert score == pytest.approx(.15 + .5 * prior + .03 + .02, abs=.025)
            assert interval[0] < score < interval[1]
            predictions.append(score)
        assert predictions[1] - predictions[0] == pytest.approx(.35, abs=.025)
        with pytest.raises(ValueError, match='not hidden'):
            metadata.predict(posterior, {**rows[0], 'artists': [999]})
    assert (joint._design, joint.terms, joint._factor) == (original_design, original_terms, original_factor)


def test_fixed_offset_preserves_metadata_gradient_and_learns_shared_correction():
    rows = []
    for artist in range(1, 5):
        for prior in (.2, .4, .6, .8):
            for source in ('provider_measurement/control', 'audio_measurement/control'):
                rows.append({'recording': len(rows) + 1, 'measurement': len(rows) + 1,
                    'artists': [artist], 'genre': 'control', 'album': None, 'source': source,
                    'score': prior + .04 + (artist - 2.5) * .01,
                    'support': .8, 'metadata_prior': prior})
    original = joint._design, joint.terms, joint._factor
    with offset.design_context():
        posterior = joint.Posterior(offset.fit(rows, 'relax'))
        assert ('baseline', 'metadata-prior') not in posterior.features
        predictions = []
        for prior in (.2, .8):
            score, interval = metadata.predict(posterior, {'recording': 1001, 'artists': [999],
                'genre': 'control', 'album': None, 'metadata_prior': prior})
            assert score == pytest.approx(prior + .04, abs=.01)
            assert interval[0] < score < interval[1]
            predictions.append(score)
        assert predictions[1] - predictions[0] == pytest.approx(.6, abs=1e-12)
        with pytest.raises(ValueError, match='not hidden'):
            metadata.predict(posterior, rows[0])
        with pytest.raises(ValueError, match='design and response offset disagree'):
            metadata.fit(rows, 'relax')
    assert (joint._design, joint.terms, joint._factor) == original


def test_source_by_genre_bias_is_shared_instead_of_independent_song_noise():
    rows = []
    for genre in range(6):
        for artist in range(1, 5):
            for family, sign in (('audio', -1), ('provider', 1)):
                for prior in (.3, .5, .7):
                    rows.append({'recording': len(rows) + 1, 'measurement': len(rows) + 1,
                        'artists': [artist], 'genre': str(genre), 'album': None,
                        'source': family + '_measurement/control', 'support': .8, 'metadata_prior': prior,
                        'score': prior + .15 * sign * (-1 if genre % 2 else 1) + (artist - 2.5) * .005})
    original = joint._design, joint.terms, joint._factor, joint.COMPONENTS, joint.INITIAL
    with source_genre.design_context():
        value = source_genre.fit(rows, 'relax')
        assert value['variances']['source_genre'] > .01
        assert max(value['diagnostics']['residual_by_family'].values()) < .001
        posterior = joint.Posterior(value)
        for genre in ('0', '1'):
            score, interval = source_genre.predict(posterior, {'recording': 1001, 'artists': [999],
                'genre': genre, 'album': None, 'metadata_prior': .6})
            assert score == pytest.approx(.6, abs=.02)
            assert interval[0] < .45 and interval[1] > .75
    assert (joint._design, joint.terms, joint._factor, joint.COMPONENTS, joint.INITIAL) == original


def test_exact_marginal_gradient_matches_dense_observation_likelihood():
    from scipy.sparse import csr_matrix, csc_matrix
    from scipy.sparse.linalg import splu
    from scipy.special import expit, logit
    rng = np.random.default_rng(819)
    matrix = rng.normal(size=(13, 7))
    kinds = np.asarray([-1, -1, 0, 0, 1, 1, 1])
    families = np.asarray([2, 3] * 6 + [2])
    supports = np.where(families == 2, rng.normal(size=13), 0.)
    response, noise = rng.normal(size=13), rng.uniform(0, .01, size=13)
    parameters = np.r_[np.log([.04, .02, .03, .07]), -.8]
    def inputs(parameters):
        variances = np.exp(parameters[:-1])
        prior = np.where(kinds < 0, .25, variances[np.maximum(kinds, 0)])
        probability = (variances[families] - 1e-5) / (.25 - 1e-5)
        residual = 1e-5 + (.25 - 1e-5) * expit(logit(probability) + parameters[-1] * supports)
        return variances, prior, residual
    def loss(parameters):
        _variances, prior, residual = inputs(parameters)
        covariance = np.diag(residual + noise) + (matrix * prior) @ matrix.T
        return .5 * (np.linalg.slogdet(covariance)[1]
            + response @ np.linalg.solve(covariance, response) + len(response) * np.log(2 * np.pi))
    variances, prior, residual = inputs(parameters)
    diagonal = residual + noise
    precision = (matrix.T / diagonal) @ matrix + np.diag(1 / prior)
    factor = splu(csc_matrix(precision))
    mean = factor.solve(matrix.T @ (response / diagonal))
    actual = derivatives.gradient(csr_matrix(matrix), factor, mean, response, diagonal,
        kinds, variances, families, supports, residual)
    steps = np.eye(len(parameters)) * 1e-5
    expected = np.asarray([(loss(parameters + step) - loss(parameters - step)) / 2e-5 for step in steps])
    assert actual == pytest.approx(expected, abs=2e-7, rel=2e-7)
    covariance, leverage = derivatives.inverse_diagonals(csr_matrix(matrix), factor)
    inverse = np.linalg.inv(precision)
    assert covariance == pytest.approx(np.diag(inverse), abs=1e-12)
    assert leverage == pytest.approx(np.diag(matrix @ inverse @ matrix.T), abs=1e-12)


def test_dimensions_transfer_when_scalar_heuristic_contains_no_information():
    rows = []
    for artist in range(1, 5):
        for groove in (.1, .3, .5, .7, .9):
            for density in (.2, .8):
                rows.append({'recording': len(rows) + 1, 'measurement': len(rows) + 1,
                    'artists': [artist], 'genre': 'control', 'album': None,
                    'source': 'provider_measurement/control', 'support': .4,
                    'metadata_prior': .5,
                    'covariates': {'dimension:groove': groove - .5, 'dimension:density': density - .5},
                    'score': .2 + .5 * groove - .1 * density + (artist - 2.5) * .01})
    original = joint._design, joint.terms, joint._factor
    with descriptors.design_context():
        posterior = joint.Posterior(metadata.fit(rows, 'cruise'))
        predictions = []
        for groove in (.1, .9):
            score, interval = metadata.predict(posterior, {'recording': 1001,
                'artists': [999], 'genre': 'control', 'album': None, 'metadata_prior': .5,
                'covariates': {'dimension:groove': groove - .5, 'dimension:density': 0.}})
            assert score == pytest.approx(.15 + .5 * groove, abs=.025)
            assert interval[0] < score < interval[1]
            predictions.append(score)
        assert predictions[1] - predictions[0] == pytest.approx(.4, abs=.025)
    assert (joint._design, joint.terms, joint._factor) == original


def test_absent_descriptors_preserve_missingness():
    values = descriptors.descriptors({})
    assert len(values) == 20
    assert set(values.values()) == {0.}


@pytest.mark.parametrize('changed', ('missing', 'code', 'candidate_sha256', 'plan_sha256'))
def test_descriptor_reserved_predictions_require_exact_prospective_seal(tmp_path, monkeypatch, changed):
    protocol, output = tmp_path / 'protocol.json', tmp_path / 'result.json'
    protocol.write_text('{}')
    plan = {'candidate_sha256': 'frozen-candidate', 'criteria': {}}
    monkeypatch.setattr(transfer, 'load', lambda path: (plan, {}, {}))
    def forbidden(*args, **kwargs):
        pytest.fail('Fitting began before reserved-candidate validation')
    monkeypatch.setattr(metadata, 'fit', forbidden)
    candidate_seal = None
    if changed != 'missing':
        candidate_seal = tmp_path / 'seal.json'
        value = {'format': transfer.FORMAT + '/seal',
            'plan_sha256': transfer.evaluation.digest(protocol.read_bytes()),
            'candidate_sha256': plan['candidate_sha256'], 'code': transfer.identity(), 'criteria': {}}
        value[changed] = 'changed'
        candidate_seal.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='prospective candidate seal|differs from its seal'):
        transfer.run(protocol, output, 'unseen', 0, 'relax', 'reserved', candidate_seal)
    assert not output.exists()


@pytest.mark.parametrize('changed', ('human-original', 'dropped-original', 'numeric-conditioning', 'fold'))
def test_transfer_capture_rejects_human_leakage_and_changed_holdouts(tmp_path, changed):
    from copy import deepcopy
    source, candidate = tmp_path / 'labels.json', tmp_path / 'candidate.json'
    profile = {'content': {'library': {'genre': 'ambient'}, 'discovered': None},
               'sources': [], 'overrides': {}, 'truncated': False}
    originals = [{'recording': recording, 'artists': [recording], 'genre': 'ambient',
                  'album': None, 'score': .4, 'support': 1., 'measurement': recording,
                  'source': 'audio_measurement/control'} for recording in (1, 2)]
    common = {'criteria': transfer.evaluation.CRITERIA, 'candidate': joint.configuration(),
              'profile_mask': transfer.evaluation.PROFILE_MASK}
    humans = {**common, 'format': transfer.labels.FORMAT, 'label_policy': transfer.labels.LABEL_POLICY,
              'human_targets': [{'recording': 2}], 'metadata': {'1': {}, '2': {}},
              'assignments': {str(i): {'cluster': f'artist:{i}', 'known': i, 'unseen': i} for i in (1, 2)},
              'profile_inputs': {'1': deepcopy(profile), '2': deepcopy(profile)},
              'rows': {vibe: deepcopy(originals) for vibe in transfer.home_vibe.PRESETS}}
    fitted = {**common, 'format': metadata.FORMAT, 'human_recordings_excluded': [2],
              'assignments': {'1': deepcopy(humans['assignments']['1'])},
              'profile_inputs': {'1': deepcopy(profile)},
              'rows': {vibe: [deepcopy(originals[0])] for vibe in transfer.home_vibe.PRESETS}}
    if changed == 'human-original':
        fitted['rows']['relax'].append(deepcopy(originals[1]))
    elif changed == 'dropped-original':
        fitted['rows']['relax'] = []
    elif changed == 'numeric-conditioning':
        fitted['profile_inputs']['1']['overrides']['energy'] = .9
    else:
        fitted['assignments']['1']['unseen'] = 4
    source.write_text(json.dumps(humans))
    fitted['source_sha256'] = transfer.evaluation.digest(source.read_bytes())
    candidate.write_text(json.dumps(fitted))
    with pytest.raises(ValueError, match='Human recording|every nonhuman original|observations leaked|folds changed'):
        transfer.inputs(source, candidate, 'metadata')


def test_correlated_recording_blocks_match_independent_dense_covariance():
    owners = np.array([0, 0, 1, 2, 2])
    noise, track = np.array([.01, .04, .03, .02, .05]), .06
    expected = np.diag(noise) + track * (owners[:, None] == owners[None, :])
    precision, determinant = pairs.block_precision(owners, track, noise)
    np.testing.assert_allclose(precision @ expected, np.eye(5), atol=1e-12)
    assert determinant == pytest.approx(np.linalg.slogdet(expected)[1], abs=1e-12)
    # Treating the pair as independent would count its shared track twice.
    assert precision[0, 1] < 0


def test_paired_source_bias_and_recording_counts_are_identified_without_song_replication():
    rng = np.random.default_rng(819)
    rows = []
    for artist in range(1, 9):
        for prior in (.1, .3, .5, .7, .9):
            latent = .18 + .5 * prior + (artist - 4.5) * .008 + rng.normal(0, .03)
            rows.append({'recording': len(rows) + 1, 'measurement': len(rows) + 1,
                'artists': [artist], 'genre': 'control', 'album': None,
                'source': 'audio_measurement/control', 'score': latent + .12 + rng.normal(0, .02),
                'support': 1., 'metadata_prior': prior,
                'replicate': {'source': 'provider_measurement/control', 'support': .4,
                    'score': latent - .06 + rng.normal(0, .04)}})
    original = joint._design, joint._ordering, joint.terms, joint._factor
    with pairs.design_context():
        value = pairs.fit(rows, 'cruise')
        posterior = joint.Posterior(value)
        assert value['diagnostics']['recordings'] == 40
        assert value['diagnostics']['measurement_observations'] == 80
        assert len(posterior.recordings) == 40
        source_bias = posterior.mean[posterior.features['source', 'provider_measurement/control']] \
            - posterior.mean[posterior.features['source', 'audio_measurement/control']]
        assert source_bias == pytest.approx(-.18, abs=.025)
        score, interval = pairs.predict(posterior, {'recording': 1001, 'artists': [999],
            'genre': 'control', 'album': None, 'metadata_prior': .5})
        assert score == pytest.approx(.55, abs=.035)
        assert interval[0] < .55 < interval[1]
        fallback, risk = pairs.predict(posterior, {'recording': 1001, 'artists': [999],
            'genre': None, 'album': None, 'metadata_prior': .2})
        assert fallback == pytest.approx(.2, abs=1e-12)
        assert risk[0] < fallback < risk[1]
        with pytest.raises(ValueError, match='not hidden'):
            pairs.predict(posterior, rows[0])
    assert (joint._design, joint._ordering, joint.terms, joint._factor) == original


def test_source_assignment_counts_artist_dependence_with_fixed_settings():
    def estimate(clusters):
        _loss, posterior = source_assignment.genre_posteriors({'control': clusters}, .1, 10., .5)
        return posterior['control']['audio_mean']
    one = estimate({'artist:1': [1, 1]})
    same = estimate({'artist:1': [10, 10]})
    diverse = estimate({f'artist:{artist}': [1, 1] for artist in range(10)})
    assert one < same < diverse
    assert diverse == pytest.approx(.55, abs=1e-10)
    value = {'prevalence': .1, 'dependence': .5, 'artists': {'1': 'artist:1'},
        'genres': [{'genre': 'control', 'audio_mean': same, 'clusters': {'artist:1': [10, 10]}}]}
    assert source_assignment.probability(value, [999], 'control') == same
    assert source_assignment.probability(value, [1], 'control') > same
    assert source_assignment.probability(value, [999], 'new-genre') == .1


def test_within_model_ranking_is_invariant_to_between_model_calibration():
    # Two models each order all ten heldouts perfectly. Their different score
    # origins must not turn that into an inferior served recommendation list.
    tasks = []
    for fold in range(2):
        rows = [{'recording': fold * 10 + i, 'cluster': f'artist:{fold * 10 + i}',
                 'truth': .8 - i * .05, 'baseline': .8 - i * .05,
                 'candidate': .8 - i * .05 - fold * .3, 'interval90': [0., 1.]}
                for i in range(10)]
        tasks.append({'fold': fold, 'outcome': 'complete', 'rows': rows})
    values = ranking.within_model_metrics(tasks)
    assert values['baseline_ndcg10'] == pytest.approx(1.)
    assert values['candidate_ndcg10'] == pytest.approx(1.)
    pooled = ranking.evaluation.metrics([row for task in tasks for row in task['rows']])
    assert pooled['candidate_ndcg10'] < pooled['baseline_ndcg10'] - .01


def test_cluster_weighted_ranking_matches_explicit_whole_cluster_resampling():
    from collections import Counter
    rows = [{'recording': i, 'cluster': f'artist:{i // 2}', 'truth': .9 - i * .07,
             'baseline': .3 + i * .02, 'candidate': .8 - i * .03,
             'interval90': [0., 1.]} for i in range(20)]
    weights = Counter({'artist:0': 2, 'artist:2': 3, 'artist:4': 1, 'artist:7': 1})
    resampled = [row for row in rows for _ in range(weights[row['cluster']])]
    actual = ranking.within_model_metrics([{'fold': 0, 'outcome': 'complete', 'rows': rows}], weights)
    expected = ranking.evaluation.metrics(resampled)
    for field in ('recordings', 'baseline_mae', 'candidate_mae', 'baseline_ndcg10',
                  'candidate_ndcg10', 'coverage90', 'interval_width'):
        assert actual[field] == pytest.approx(expected[field], abs=1e-12)
    with pytest.raises(ValueError, match='Duplicate target'):
        ranking.within_model_metrics([{'fold': 0, 'outcome': 'complete', 'rows': rows},
                                     {'fold': 1, 'outcome': 'complete', 'rows': rows}])


def test_new_fold_protocol_keeps_connected_collaborations_wholly_unseen():
    rows = [{'recording': 1, 'artists': [1]}, {'recording': 2, 'artists': [1, 2]},
            {'recording': 3, 'artists': [2]}, {'recording': 4, 'artists': [3]}]
    original = selection.evaluation.assignments(rows)
    changed = selection.fold_assignment(original)
    assert {changed[str(i)]['unseen'] for i in (1, 2, 3)} == {changed['1']['unseen']}
    assert len({changed[str(i)]['cluster'] for i in (1, 2, 3)}) == 1
    assert changed == selection.fold_assignment(dict(reversed(list(original.items()))))
    assert changed == selection.fold_assignment(changed)
    plan = {'rows': {'relax': rows}, 'assignments': changed}
    hidden = selection.expected_targets(plan, 'unseen', changed['1']['unseen'], 'relax')
    assert {1, 2, 3} <= hidden.keys()
    # A known-artist target needs a different measured song in its artist group.
    plan['assignments'] = {'1': {'known': 0}, '2': {'known': 1},
                           '3': {'known': 1}, '4': {'known': 0}}
    assert selection.expected_targets(plan, 'known', 0, 'relax').keys() == {1}


def test_single_model_human_transfer_excludes_both_partitions_artist_components():
    humans = {'human_targets': [{'recording': 1, 'partition': 'development'},
                               {'recording': 4, 'partition': 'reserved'}],
              'assignments': {'1': {'cluster': 'shared'}, '2': {'cluster': 'shared'},
                              '3': {'cluster': 'shared'}, '4': {'cluster': 'reserved'},
                              '5': {'cluster': 'reserved'}, '6': {'cluster': 'other'}}}
    assert transfer.hidden_recordings(humans, 'single-model', 'unseen', 0) == {1, 2, 3, 4, 5}
    assert transfer.hidden_recordings(humans, 'single-model', 'known', 0) == set()
    with pytest.raises(ValueError, match='only fold zero'):
        transfer.hidden_recordings(humans, 'single-model', 'unseen', 1)
