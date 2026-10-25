"""Synthetic policy reference cases, not human listening labels or calibration."""
import copy

import pytest

from yt_playlist.rec import musical_profiles as profiles


def source(*, scope='recording', provider='acousticbrainz', entity='recording-id', **values):
    return profiles.source_profile({'provider': provider, 'scope': scope, 'entity_id': entity,
        'observations': {field: {'value': value, 'provenance': {
            'match_evidence': {'method': 'supplied_recording_mbid'}}} for field, value in values.items()}})


def dimension(sources=(), name='intensity', **kwargs):
    return profiles.resolve(sources, **kwargs)['dimensions'][name]


def test_unknown_is_unknown_and_genres_never_invent_tempo_or_vocals():
    assert all(value['value'] is None for value in profiles.resolve([])['dimensions'].values())
    assert dimension(genre='Doom Metal')['value'] > dimension(genre='Ambient')['value']
    for genre in ('Doom Metal', 'Techno', 'Jazz', 'Ambient'):
        for name in ('tempo', 'instrumental', 'acoustic', 'happiness', 'sadness'):
            assert dimension(name=name, genre=genre)['value'] is None
    assert profiles.resolve([], genre='Metal')['reliability']['status'] == 'uncalibrated'


def test_slow_heavy_music_can_be_more_intense_than_fast_relaxed_music():
    heavy = source(bpm=65, mood_aggressive=.95, mood_party=.2, mood_relaxed=.05)
    light = source(bpm=170, mood_aggressive=.05, mood_party=.2, mood_relaxed=.95)
    assert dimension([heavy])['value'] > dimension([light])['value']
    assert dimension([heavy], 'tempo')['value'] < dimension([light], 'tempo')['value']
    for bpm in (65, 130, 260):
        assert dimension([source(bpm=bpm)])['value'] is None
    tempo = dimension([heavy], 'tempo')
    assert tempo['evidence'][0]['evidence'][0]['alternatives_bpm'] == [32.5, 65, 130]


def test_raw_probability_match_weight_and_reliability_are_separate():
    value = dimension([source(mood_aggressive=.99)], 'aggression')
    assert value['value'] == pytest.approx(.99)
    assert value['support'] == pytest.approx(.45)
    assert value['support'] != .99


def test_missing_correlated_components_stay_neutral_and_legacy_energy_is_not_an_extra_vote():
    raw = source(mood_aggressive=.9)
    assert .5 < dimension([raw])['value'] < .7
    duplicate = source(mood_aggressive=.9, energy=1, danceability=1)
    assert dimension([raw]) == dimension([duplicate])
    assert dimension([source(energy=1)])['value'] is None


def test_raw_component_override_and_retraction_also_change_derived_intensity():
    raw = source(mood_aggressive=.95, mood_party=.8, mood_relaxed=.05)
    previous = dimension([raw])['value']
    corrected = dimension([raw], overrides={'mood_aggressive': .05})['value']
    withdrawn = dimension([raw], overrides={name: None for name in
        ('mood_aggressive', 'mood_party', 'mood_relaxed')})
    assert corrected < previous
    assert withdrawn['value'] is None
    assert dimension([raw], overrides={'energy': None}, genre='Metal')['value'] is None
    assert dimension([raw], overrides={'energy': .1})['value'] == .1


def test_scoped_multi_tags_are_conservative_and_personal_context_is_ignored():
    artist = source(scope='artist', tags=[{'name': 'Ambient'}, {'name': 'seen live', 'count': 1000},
        {'name': 'instrumental'}, {'name': 'favorites'}, {'name': 'workout'}])
    recording = source(tags=[{'name': 'Metal'}])
    assert dimension([artist])['support'] <= .1
    assert dimension([artist], 'instrumental')['value'] is None
    assert dimension([recording, artist]) == dimension([recording])
    alias = source(tags=[{'name': 'drum and bass'}, {'name': 'Drum & Bass'}, {'name': 'Electronic'}])
    single = source(tags=[{'name': 'Drum & Bass'}])
    assert dimension([alias])['support'] <= dimension([single])['support']


def test_cached_resolver_is_deterministic_independent_of_pool_and_does_not_mutate_inputs():
    sources = [source(mood_aggressive=.9, bpm=80), source(scope='artist', tags=[{'name': 'Metal'}])]
    before = copy.deepcopy(sources)
    expected = profiles.resolve(sources)
    for distractor in ('Ambient', 'Metal', 'Techno', 'Jazz'):
        profiles.resolve([], genre=distractor)
        assert profiles.resolve(sources) == expected
    assert sources == before


def test_unverified_name_match_cannot_supply_a_profile_and_nonfinite_values_stay_unknown():
    value = source(mood_aggressive=1, tags=[{'name': 'Metal'}])
    for observation in value['observations'].values():
        observation['provenance']['match_evidence'] = {'method': 'name_query', 'title_artist_match': False}
    value = profiles.source_profile(value)
    assert dimension([value])['value'] is None
    for bad in (True, -1, 2, 'unknown'):
        assert dimension([source(mood_aggressive=bad)])['value'] is None
    for bad in (float('nan'), float('inf')):
        assert profiles.number(bad) is None
        with pytest.raises(ValueError):
            source(mood_aggressive=bad)


def test_explicit_genre_replaces_inherited_tags_without_overriding_recording_classifiers():
    artist = source(scope='artist', tags=[{'name': 'Metal'}])
    assert dimension([artist], overrides={'genre': 'Ambient'})['value'] < .5
    assert dimension([artist], overrides={'genre': None})['value'] is None
    assert dimension([source(mood_aggressive=.95), artist], overrides={'genre': 'Ambient'})['value'] > .5


def test_retracted_native_values_cannot_be_resurrected_from_legacy_selected_columns():
    selected = {'energy': .9, 'mood_aggressive': .9, 'bpm': 160}
    raw = source(mood_aggressive=None, bpm=None)
    resolved = profiles.resolve([raw], selected=selected)['dimensions']
    assert all(resolved[name]['value'] is None for name in ('intensity', 'aggression', 'tempo'))


def test_selected_recording_fields_precede_genre_priors_but_keep_weak_support():
    for sources in ([], [source(scope='artist', tags=[{'name': 'Metal'}])]):
        resolved = profiles.resolve(sources, genre='Metal', selected={'energy': .05, 'danceability': .1})['dimensions']
        assert resolved['intensity']['value'] < .5
        assert resolved['intensity']['support'] == .35
        assert resolved['groove']['value'] == .1
    # Native recording evidence and an explicit retraction still own the field.
    raw = source(mood_aggressive=.95, mood_party=.9, mood_relaxed=.05)
    assert dimension([raw], selected={'energy': .05}, genre='Ambient')['value'] > .5
