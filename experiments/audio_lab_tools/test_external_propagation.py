"""Independent checks of the frozen library propagation experiment."""
import pytest

from experiments.audio_lab_tools import external_propagation as replay


def fixture_library():
    return {'measurements': [
        {'id': 1, 'recording_id': 10, 'vibe': 'relax', 'score': .9, 'support': 1., 'source': 'provider_measurement'},
        {'id': 2, 'recording_id': 11, 'vibe': 'relax', 'score': .1, 'support': 1., 'source': 'audio_measurement'}],
        'recording_vibes': [
            {'recording_id': 10, 'vibe': 'relax', 'score': .9, 'support': 1., 'source': 'provider_measurement',
             'measurement_id': 1, 'artist_key': '[1]', 'subgenre_key': 'psybient'},
            {'recording_id': 11, 'vibe': 'relax', 'score': .1, 'support': 1., 'source': 'audio_measurement',
             'measurement_id': 2, 'artist_key': '[1]', 'subgenre_key': 'psybient'},
            {'recording_id': 12, 'vibe': 'relax', 'score': .99, 'support': .5, 'source': 'artist_genre',
             'measurement_id': None, 'artist_key': '[1]', 'subgenre_key': 'psybient'}]}


def test_only_original_current_measurements_seed_and_heldout_self_is_removed():
    metadata, observations = replay.current_inputs(fixture_library(), {'provider_measurement', 'audio_measurement'})
    assert set(observations) == {10, 11}
    statistics = replay.cohorts(metadata, observations, {11})
    estimated = replay.resolve(metadata[11], statistics)['relax']
    assert estimated['recordings'] == 1
    assert estimated['source'] == 'artist_genre'
    assert estimated['score'] == pytest.approx(.66)
    assert replay.retraction_check(metadata, observations)['maximum_statistic_error'] < 1e-12


def test_provider_only_seeds_and_missing_metadata_abstains():
    metadata, observations = replay.current_inputs(fixture_library(), {'provider_measurement'})
    assert set(observations) == {10}
    fits = replay.resolve({'artist': None, 'genre': None}, replay.cohorts(metadata, observations))
    assert all(fit == {'score': .5, 'support': 0., 'source': 'heuristic'} for fit in fits.values())


def test_overlapping_canonical_artist_credits_share_human_fold_group():
    rows = [{'artist': 'One'}, {'artist': 'One with guest'}, {'artist': 'Other'}]
    groups = replay.human_groups(rows, [10, 11, 12], {
        10: {'artist': '[1]'}, 11: {'artist': '[1,2]'}, 12: {'artist': '[3]'}})
    assert groups[0] == groups[1]
    assert groups[0] != groups[2]
