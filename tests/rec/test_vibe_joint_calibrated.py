"""Independent dense checks for the published masked-metadata model format."""
import math

import numpy as np
import pytest
from scipy.optimize import brentq
from scipy.special import ndtr

from yt_playlist.rec import vibe_joint_model as joint


def rows():
    return [
        {'recording': 1, 'measurement': 1, 'artists': [1], 'genre': 'deep', 'album': 'a',
         'score': .8, 'metadata_prior': .9, 'support': .8, 'source': 'provider_measurement/a'},
        {'recording': 2, 'measurement': 2, 'artists': [2], 'genre': 'other', 'album': 'a',
         'score': .3, 'metadata_prior': .2, 'support': .4, 'source': 'provider_measurement/a'},
        {'recording': 3, 'measurement': 3, 'artists': [1, 2], 'genre': 'deep', 'album': 'b',
         'score': .6, 'metadata_prior': .6, 'support': .8, 'source': 'provider_measurement/a'},
        {'recording': 4, 'measurement': 4, 'artists': [3], 'genre': 'other', 'album': 'b',
         'score': .7, 'metadata_prior': .4, 'support': 1., 'source': 'audio_measurement/b'},
    ]


def test_calibrated_fit_and_predict_match_independent_dense_mixture():
    training = rows()
    # Construct kernels independently: fractional credits, artist-nested albums,
    # two baseline coefficients, source identity, and independent residual risk.
    def features(row):
        artists, genre, album = row['artists'], row['genre'], row['album']
        return {'artist': {a: 1 / len(artists) for a in artists}, 'genre': {genre: 1.},
                'interaction': {(a, genre): 1 / len(artists) for a in artists},
                'album': {(a, album): 1 / len(artists) for a in artists},
                'source': {row['source']: 1.}}
    def covariance(left, right):
        a, b = features(left), features(right)
        return (.25 * (1 + (left['metadata_prior'] - .5) * (right['metadata_prior'] - .5))
                + sum(joint.INITIAL[kind] * sum(weight * b[kind].get(key, 0.)
                    for key, weight in values.items()) for kind, values in a.items()))
    kernel = np.asarray([[covariance(a, b) for b in training] for a in training])
    kernel += np.eye(4) * joint.INITIAL['residual']
    response = np.asarray([r['score'] - .5 for r in training])
    alpha = np.linalg.solve(kernel, response)
    value = joint.fit(training, 'relax', fixed=joint.INITIAL)
    assert value['format'] == 4
    expected_loss = .5 * (response @ alpha + np.linalg.slogdet(kernel)[1] + 4 * math.log(2 * math.pi))
    assert value['diagnostics']['negative_log_likelihood'] == pytest.approx(expected_loss, abs=1e-10)
    posterior = joint.Posterior(value)
    predicted = posterior.predict([1, 2], 'deep', album='new', recording=99, metadata_prior=.8)
    targets = [{'artists': [1, 2], 'genre': 'deep', 'album': 'new', 'metadata_prior': .8,
                'source': source} for source in ('provider_measurement/a', 'audio_measurement/b')]
    weights = np.asarray([.75, .25])
    crosses = np.asarray([[covariance(q, row) for row in training] for q in targets])
    means = .5 + crosses @ alpha
    variances = np.asarray([covariance(q, q) - cross @ np.linalg.solve(kernel, cross)
                            + joint.INITIAL['residual'] for q, cross in zip(targets, crosses)])
    cross_mean = weights @ crosses
    mean_prior = sum(weights[i] * weights[j] * covariance(a, b)
                     for i, a in enumerate(targets) for j, b in enumerate(targets))
    expected_mean = weights @ means
    assert predicted['score'] == pytest.approx(expected_mean, abs=1e-10)
    assert predicted['mean_variance'] == pytest.approx(
        mean_prior - cross_mean @ np.linalg.solve(kernel, cross_mean), abs=1e-10)
    assert predicted['new_track_variance'] == pytest.approx(weights @ (
        variances + (means - expected_mean) ** 2), abs=1e-10)
    expected_interval = [brentq(lambda x: weights @ ndtr((x - means) / np.sqrt(variances)) - p,
                               -2., 3.) for p in (.05, .95)]
    assert predicted['interval90'] == pytest.approx(np.clip(expected_interval, 0., 1.), abs=1e-10)
    with pytest.raises(ValueError, match='Target measurement was not hidden'):
        posterior.predict([1], 'deep', recording=1, metadata_prior=.8)
    with pytest.raises(ValueError, match='masked metadata prior'):
        posterior.predict([1], 'deep')


def test_format_four_rejects_incomplete_or_invalid_conditioning():
    training = rows()
    training[0].pop('metadata_prior')
    with pytest.raises(ValueError, match='Every calibrated original'):
        joint.fit(training, 'relax')
    training = rows()
    training[0]['metadata_prior'] = float('nan')
    with pytest.raises(ValueError, match='Invalid calibrated original'):
        joint.fit(training, 'relax')
    value = joint.fit(rows(), 'relax', fixed=joint.INITIAL)
    value['diagnostics']['support_slope'] = 100.
    with pytest.raises(ValueError, match='Invalid calibrated residual'):
        joint.Posterior(value)


def test_format_four_accumulation_accounts_for_artist_dependence_and_track_variation():
    def original(recording, *, artist=1, genre='deep', score=.7, album=None):
        return {'recording': recording, 'measurement': recording, 'artists': [artist],
                'genre': genre, 'album': album, 'score': score, 'metadata_prior': .5,
                'support': 1., 'source': 'audio_measurement/control'}

    def predict(training, *, artist=1, genre='deep'):
        posterior = joint.Posterior(joint.fit(training, 'relax', fixed=joint.INITIAL))
        return posterior.predict([artist] if artist else [], genre, metadata_prior=.5)

    one = [original(1)]
    ten = [original(recording) for recording in range(1, 11)]
    first, agreeing = predict(one), predict(ten)
    assert .5 < first['score'] < agreeing['score'] <= .7
    assert agreeing['mean_variance'] < first['mean_variance']
    assert agreeing['new_track_variance'] - agreeing['mean_variance'] == pytest.approx(joint.INITIAL['residual'])
    assert agreeing['new_track_variance'] > joint.INITIAL['residual']
    assert predict(ten + ten) == agreeing
    assert predict([*ten, original(11, score=.1)])['score'] < agreeing['score']
    with pytest.raises(ValueError, match='Conflicting original'):
        predict([*one, original(1, score=.1)])
    diverse = [original(recording, artist=recording) for recording in range(1, 11)]
    assert predict(diverse, artist=999)['mean_variance'] < predict(ten, artist=999)['mean_variance']
    specific = [original(1, score=.9)]
    broad = [original(2, genre='other', score=.1), original(3, artist=2, score=.1)]
    assert predict(specific + broad)['score'] < predict(specific)['score']
    clustered = [original(recording, album='same') for recording in range(1, 11)]
    independent = [original(recording, album=str(recording)) for recording in range(1, 11)]
    assert predict(independent)['mean_variance'] < predict(clustered)['mean_variance']
    assert predict(one, artist=None, genre=None) is None
    assert predict(one, artist=999, genre='unseen')['mean_variance'] > first['mean_variance']


def test_masked_prior_ignores_own_numeric_inputs_and_preserves_artist_descriptions():
    import json
    from yt_playlist.rec import musical_preparation, musical_profiles

    source = musical_profiles.source_profile({'provider': 'control', 'scope': 'artist', 'entity_id': '1',
        'observations': {'tags': {'value': [{'name': 'Ambient'}],
                                  'provenance': {'match_evidence': {'method': 'title_artist_match'}}}}})
    content = {'library': {'genre': 'Detroit Techno', 'year': None,
                           'audio': {'mood_relaxed': .99, 'mood_happy': .9}}, 'discovered': None}
    def prepare(value, sources, overrides, *, masked=True):
        operation = musical_preparation.masked_recordings if masked else musical_preparation.recordings
        return operation(None, [('target', 0, json.dumps(value), None, True)],
                         inputs={'target': (sources, overrides, False)})[0][1]
    overrides = {'energy': .99, 'mood_relaxed': .01, 'genre': 'Psybient'}
    actual = prepare(content, [source], overrides)
    content['library']['audio'] = {}
    assert actual == prepare(content, [source], {'genre': 'Psybient'})
    assert actual != prepare(content, [], {'genre': 'Psybient'})
    assert actual != prepare(content, [source], overrides, masked=False)
    with pytest.raises(ValueError, match='source admission before source limits'):
        prepare(content, [{**source, 'scope': 'recording'}], overrides)
