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
