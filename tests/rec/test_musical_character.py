"""Missing musical evidence must remain usable and honestly attributed."""
import pytest

from yt_playlist.rec import musical_character as character
from yt_playlist.rec import vibe_joint_state
from yt_playlist.rec.vibe_score_policy import joint_suffix, serving_prefix


def specialist_vibes(fits):
    return {'inputs_version': serving_prefix() + 'test-specialists', 'intensity_fits': fits}


@pytest.mark.parametrize('profile', [
    None, {}, {'vibes': None}, {'vibes': {'fits': None}},
    {'vibes': {'fits': {}}}, {'vibes': {'fits': {'energize': {}}}},
    {'vibes': {'fits': {'energize': None, 'release': {}}}},
    {'vibes': {'fits': {'release': {'score': None, 'source': 'audio_measurement'}}}},
])
def test_missing_intensity_is_unknown_while_serving_remains_neutral(profile):
    item = {'musical': profile}
    assert character.intensity_evidence(item) == (None, 0)
    assert character.intensity(item) == .5
    context = character.provenance(item)
    assert context['intensity'] is None and context['intensity_support'] == 0
    assert context['intensity_source'] is None and context['intensity_vibe'] is None


@pytest.mark.parametrize('fit', [
    {'score': True, 'source': 'audio_measurement', 'support': 1.},
    {'score': 1., 'source': 'heuristic', 'support': 1.},
    {'score': 1., 'source': 'provider_measurement', 'support': 1.},
    {'score': 1., 'source': 'acousticbrainz_estimate', 'support': 1.},
    {'score': float('nan'), 'source': 'audio_measurement'},
    {'score': float('inf'), 'source': 'audio_measurement'},
    {'score': -1., 'source': 'audio_measurement'},
    {'score': 2., 'source': 'audio_measurement'},
    {'score': None, 'source': 'audio_measurement'},
    {'score': '1', 'source': 'audio_measurement'}, 'incomplete',
])
def test_full_on_requires_a_valid_specialist_fit(fit):
    item = {'musical': {'dimensions': {'intensity': {'value': 1., 'support': .75}},
                        'vibes': specialist_vibes({'energize': fit})}}
    assert character.dimension(item, 'intensity') == (1., .75)
    assert character.intensity_evidence(item) == (None, 0)
    context = character.provenance(item)
    assert context['intensity'] is None and context['intensity_support'] == 0
    assert context['intensity_source'] is None and context['intensity_vibe'] is None


@pytest.mark.parametrize('energize,release,winner', [(.1, .95, 'release'), (.9, .2, 'energize'),
                                                  (.8, .8, 'energize'), (0., 0., 'energize')])
def test_provenance_names_the_exact_valid_fit_used_for_scoring(energize, release, winner):
    fits = {'energize': {'score': energize, 'support': .6, 'source': 'audio_measurement'},
            'release': {'score': release, 'support': .8, 'source': 'artist_genre'}}
    vibes = specialist_vibes(fits)
    fits['release']['inputs_version'] = vibes['inputs_version'] + '/' + joint_suffix(vibe_joint_state.config_identity())
    item = {'musical': {'version': 'profile-v1', 'digest': 'digest', 'vibes': vibes}}
    expected = fits[winner]
    assert character.intensity_evidence(item) == (expected['score'], expected['support'])
    context = character.provenance(item)
    assert context['musical_version'] == 'profile-v1' and context['musical_digest'] == 'digest'
    assert context['intensity'] == expected['score'] and context['intensity_support'] == expected['support']
    assert context['intensity_source'] == 'specialist_ensemble' and context['intensity_vibe'] == winner
    assert context['intensity_ensemble']['components'] == fits


def test_missing_source_does_not_supply_full_on_evidence():
    item = {'musical': {'vibes': specialist_vibes({'release': {'score': .7, 'support': .4}})}}
    assert character.intensity_evidence(item) == (None, 0)
    context = character.provenance(item)
    assert context['intensity'] is None and context['intensity_vibe'] is None
    assert context['intensity_source'] is None


def test_retired_prepared_intensity_cannot_supply_full_on_evidence():
    item = {'musical': {'dimensions': {'intensity': {'value': .2, 'support': .8}},
        'vibes': {'inputs_version': 'recording-vibes-1/retired-shared-model',
                  'intensity_fits': {'release': {'score': .9, 'support': 1., 'source': 'audio_measurement'}}}}}
    assert character.intensity_evidence(item) == (None, 0)
    assert character.provenance(item)['intensity_ensemble'] is None


@pytest.mark.parametrize('stamp', [None, 'mixed-training-policy'])
def test_full_on_rejects_unversioned_or_superseded_predictions(stamp):
    fits = {'energize': {'score': .99, 'support': 1., 'source': 'artist_genre'}}
    vibes = specialist_vibes(fits)
    if stamp is None:
        del vibes['inputs_version']
    else:
        fits['energize']['inputs_version'] = vibes['inputs_version'] + '/' + stamp
    item = {'musical': {'vibes': vibes}}
    assert character.intensity_evidence(item) == (None, 0)
    assert character.intensity(item) == .5


def test_full_on_rejects_predictions_from_genre_alone():
    fit = {'score': .99, 'support': 1., 'source': 'genre'}
    vibes = specialist_vibes({'energize': fit})
    fit['inputs_version'] = vibes['inputs_version'] + '/' + joint_suffix(vibe_joint_state.config_identity())
    assert character.intensity_evidence({'musical': {'vibes': vibes}}) == (None, 0)


@pytest.mark.parametrize('value', [0., .1, .2, 1.])
def test_full_on_uses_specialist_score_when_character_intensity_is_corrected(value):
    item = {'musical': {'dimensions': {'intensity': {'value': value, 'support': 1., 'override': True}},
                        'vibes': specialist_vibes({'release': {'score': .885, 'support': 1.,
                                                              'source': 'audio_measurement'}})}}
    assert character.dimension(item, 'intensity') == (value, 1.)
    assert character.intensity_evidence(item) == (.885, 1.)
    assert character.intensity(item) == .885
    context = character.provenance(item)
    assert context['intensity'] == .885 and context['intensity_support'] == 1.
    assert context['intensity_source'] == 'specialist_ensemble' and context['intensity_vibe'] == 'release'


@pytest.mark.parametrize('correction', [
    {'value': None, 'support': 1., 'override': True},
    {'value': float('nan'), 'support': 1., 'override': True},
    {'value': .1, 'support': 0., 'override': True},
    {'value': .1, 'support': 1., 'override': False},
    {'value': .1, 'support': 1., 'override': 'true'},
])
def test_missing_or_invalid_character_correction_does_not_change_full_on(correction):
    item = {'musical': {'dimensions': {'intensity': correction},
                        'vibes': specialist_vibes({'release': {'score': .885, 'support': .8,
                                                              'source': 'audio_measurement'}})}}
    assert character.intensity_evidence(item) == (.885, .8)
    assert character.provenance(item)['intensity_source'] == 'specialist_ensemble'
