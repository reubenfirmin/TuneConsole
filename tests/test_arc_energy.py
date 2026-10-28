"""Journeys consume saved intensity; they cannot derive a competing definition."""
import pytest

from tests.musical_support import prepare_profiles
from yt_playlist.rec import arc_energy, home_cards, journeys, musical_character, recipes
from yt_playlist.rec.musical_profiles import resolve


def profile(value):
    return musical_character.compact(resolve([], overrides={'energy': value}))


def test_unchanged_recording_keeps_its_intensity_in_different_pools():
    profiles = {'slow_heavy': profile(.9), 'fast_gentle': profile(.2)}
    alone = arc_energy.arc_energies(['slow_heavy'], profiles)
    together = arc_energy.arc_energies(['slow_heavy', 'fast_gentle', 'unknown'], profiles)
    assert alone['slow_heavy'] == together['slow_heavy'] == .9
    assert together['fast_gentle'] == .2
    assert together['unknown'] == .5


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), True, -.1, 1.1])
def test_unknown_or_invalid_evidence_orders_neutrally(value):
    assert arc_energy.arc_energies(['k'], {'k': {'dimensions': {
        'intensity': {'value': value, 'support': 1}}}}) == {'k': .5}


def test_energy_arc_peaks_in_middle_using_the_shared_dimension():
    keys = [f'k{i}' for i in range(12)]
    values = arc_energy.arc_energies(keys, {key: profile(i / 11) for i, key in enumerate(keys)})
    order = journeys.journey_order(keys, 'energy_arc', seed=1, feat=lambda key: {
        'artist': key, 'genre': 'Metal', 'energy': values[key], 'decade': None, 'plays': 0, 'recency': 0})
    assert sum(values[key] for key in order[4:8]) > sum(values[key] for key in order[:4])
    assert sum(values[key] for key in order[4:8]) > sum(values[key] for key in order[8:])


def test_mix_and_cluster_journeys_read_prepared_intensity_without_loading_audio(store, monkeypatch):
    keys = []
    for i in range(8):
        tid = store.tracks.upsert_track(f'v{i}', f'Song {i}', f'Artist {i}', '', 200)
        store.tracks.set_track_audio(tid, energy=i / 7, bpm=180 - i * 15)
        keys.append(store.tracks.catalog_subject(tid)['identity_key'])
    prepare_profiles(store)
    def unexpected():
        pytest.fail('Journeys must not load catalog-wide audio')
    monkeypatch.setattr(store.rec_query, 'track_audio_features', unexpected)
    recipe, ordered = recipes.cluster_recipe(store, keys, journey='warm_up')
    values = musical_character.load(store, keys)
    # Journeys preserve contiguous intensity bands and space artists within each.
    assert max(musical_character.intensity({'musical': values[key]}) for key in ordered[:4]) < min(
        musical_character.intensity({'musical': values[key]}) for key in ordered[4:])
    assert recipe['musical_version'] == musical_character.VERSION
    items = [{'key': key, 'artist': f'Artist {i}'} for i, key in enumerate(keys)]
    assert [item['key'] for item in home_cards._order_by_journey(store, items, 'warm_up', recipe['dj']['seed'])] == ordered
