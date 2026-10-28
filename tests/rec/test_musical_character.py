"""Missing musical evidence must remain usable and honestly attributed."""
import pytest

from yt_playlist.rec import musical_character as character


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
    {'score': True, 'source': 'audio_measurement'},
    {'score': 1., 'source': 'heuristic'},
    {'score': float('nan'), 'source': 'audio_measurement'},
    {'score': float('inf'), 'source': 'audio_measurement'},
    {'score': -1., 'source': 'audio_measurement'},
    {'score': 2., 'source': 'audio_measurement'},
    {'score': None, 'source': 'audio_measurement'},
    {'score': '1', 'source': 'audio_measurement'}, 'incomplete',
])
def test_invalid_fit_cannot_take_credit_for_a_supported_dimension_fallback(fit):
    item = {'musical': {'dimensions': {'intensity': {'value': 1., 'support': .75}},
                        'vibes': {'fits': {'energize': fit}}}}
    assert character.intensity_evidence(item) == (1., .75)
    context = character.provenance(item)
    assert context['intensity'] == 1. and context['intensity_support'] == .75
    assert context['intensity_source'] is None and context['intensity_vibe'] is None


@pytest.mark.parametrize('energize,release,winner', [(.1, .95, 'release'), (.9, .2, 'energize'),
                                                  (.8, .8, 'energize'), (0., 0., 'energize')])
def test_provenance_names_the_exact_valid_fit_used_for_scoring(energize, release, winner):
    fits = {'energize': {'score': energize, 'support': .6, 'source': 'audio_measurement'},
            'release': {'score': release, 'support': .8, 'source': 'artist_genre'}}
    item = {'musical': {'version': 'profile-v1', 'digest': 'digest', 'vibes': {'fits': fits}}}
    expected = fits[winner]
    assert character.intensity_evidence(item) == (expected['score'], expected['support'])
    context = character.provenance(item)
    assert context['musical_version'] == 'profile-v1' and context['musical_digest'] == 'digest'
    assert context['intensity'] == expected['score'] and context['intensity_support'] == expected['support']
    assert context['intensity_source'] == expected['source'] and context['intensity_vibe'] == winner


def test_missing_source_keeps_the_original_usable_score_without_inventing_attribution():
    item = {'musical': {'vibes': {'fits': {'release': {'score': .7, 'support': .4}}}}}
    assert character.intensity_evidence(item) == (.7, .4)
    context = character.provenance(item)
    assert context['intensity'] == .7 and context['intensity_vibe'] == 'release'
    assert context['intensity_source'] is None
