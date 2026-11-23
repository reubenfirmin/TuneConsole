import numpy as np

from .acousticbrainz_study import adjust, duration_matches, fit_adjustment, provider_probabilities, resolve_recording


def test_duration_matching_admits_full_length_and_known_trim():
    assert duration_matches(141.5, 141.45)
    assert duration_matches(201.5, 141.45)
    assert not duration_matches(193.5, 141.45)


def test_adjustment_keeps_exact_specialist_score_if_a_probability_is_missing():
    provider = np.array([[0., 0.], [0.5, 0.2], [1., 0.1]])
    model = fit_adjustment(provider, np.array([-0.1, 0.1, 0.4]))
    base = np.array([0.99, 0.357, 0.244])
    values = np.array([[1., 0.1], [0., np.nan], [np.nan, np.nan]])
    predicted = adjust(base, values, model, np.isfinite(values).all(axis=1))
    np.testing.assert_array_equal(predicted, [1., 0.357, 0.244])


def test_shared_release_breaks_tie_only_with_unique_recording():
    album = {'msd_release': 'The Record', 'musicbrainz_releases': ['The Record', 'An Anthology']}
    unrelated = {'msd_release': 'The Record', 'musicbrainz_releases': ['Another Record']}
    assert resolve_recording({'studio': [album], 'other': [unrelated]}) == ('studio', 'unique_release_agreement')
    assert resolve_recording({'studio': [album], 'other': [album]}) == (None, 'ambiguous')


def test_provider_batch_handles_requested_misses_and_documented_mapping_envelope():
    submission = {'highlevel': {'voice_instrumental': {'all': {'instrumental': 0.1}},
                                'mood_relaxed': {'all': {'relaxed': 0.7}}}}
    response = {'first': {'0': submission}, 'mbid_mapping': {}}
    assert provider_probabilities(response, ['first', 'second']) == {
        'first': {'instrumental': 0.1, 'relaxed': 0.7},
        'second': {'instrumental': None, 'relaxed': None},
    }
