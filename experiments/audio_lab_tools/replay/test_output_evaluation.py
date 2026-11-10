"""Independent evidence-isolation and missing-reference checks for #587."""
import pytest

from experiments.audio_lab_tools.replay import output_evaluation as evaluation
from experiments.audio_lab_tools.replay import output_evaluation_report as reports
from yt_playlist.rec import musical_character, musical_profiles


def inventory():
    empty = musical_character.compact(musical_profiles.resolve([]))
    return {'records': {n: {'identity_key': str(n)} for n in (1, 2, 3)},
        'base': {str(n): empty for n in (1, 2, 3)},
        'metadata': {n: {'artist': '[1]', 'genre': 'ambient'} for n in (1, 2, 3)},
        'provider': {1: {'source': 'provider_measurement', 'fits': {'relax': {'score': .9, 'support': 1.}}}},
        'acoustic': {1: {'source': 'audio_measurement', 'fits': {'relax': {'score': .1, 'support': 1.}}}}}


def test_audio_is_removed_from_metadata_and_metadata_cannot_teach_signal_neighbors():
    data = inventory()
    metadata = evaluation.profiles(data, 'metadata')
    signal = evaluation.profiles(data, 'signal')
    combined = evaluation.profiles(data, 'combined')
    fit = lambda p, key: p[key]['vibes']['fits']['relax']
    assert fit(metadata, '1')['score'] == .9
    assert fit(signal, '1')['score'] == .1
    assert fit(combined, '1')['score'] == .1
    assert fit(metadata, '2')['source'] == 'artist_genre'
    assert fit(signal, '2') == {'score': .5, 'support': 0., 'source': 'heuristic'}
    assert fit(combined, '2')['recordings'] == 1
    assert fit(combined, '3')['recordings'] == 1  # No recursive vote from 2.


def test_partial_audio_keeps_provider_fallback_and_cannot_create_an_audio_vote():
    data = inventory()
    assert evaluation.profiles(data, 'combined', first_excerpts=True) == evaluation.profiles(data, 'metadata')
    signal = evaluation.profiles(data, 'signal', first_excerpts=True)
    assert all(fit['source'] == 'heuristic' for p in signal.values() for fit in p['vibes']['fits'].values())
    assert all(musical_character.intensity_evidence({'musical': p}) == (None, 0) for p in signal.values())


def test_grouping_connects_shared_artists_and_albums_transitively_without_missing_values():
    groups = reports.connected_groups([
        {'key': 'a', 'artists': [1]}, {'key': 'b', 'artists': [1, 2]},
        {'key': 'c', 'artists': [2], 'albums': ['album']}, {'key': 'd', 'albums': ['album']},
        {'key': 'e'}, {'key': 'f'}])
    assert len(set(groups[:4])) == 1
    assert len(set(groups[3:])) == 3


def test_unselected_vibes_are_unknown_and_bootstrap_resamples_paired_groups():
    profiles = {a: {'track': {'vibes': {'fits': {'relax': {'score': value}, 'release': {'score': 0.}}}}}
                for a, value in zip(evaluation.ARMS, (.6, .8, .9))}
    result = reports.positive_metrics([{'key': 'track', 'labels': ['relax']}], profiles, ['relax', 'release'])
    assert result['release']['positive_recordings'] == 0
    assert result['release']['positive_only_mae']['signal'] is None
    assert result['relax']['error_difference_vs_metadata']['combined']['mean'] == pytest.approx(-.3)
    assert result['relax']['error_difference_vs_metadata']['combined']['ci95'] is None
    assert reports.paired_interval([.2, .2, .2], [1, 1, 2])['ci95'] == pytest.approx([.2, .2])


def test_unknown_intensity_is_not_counted_as_a_neutral_measurement():
    result = reports.coverage([{'key': 'missing', 'artist': 'Someone'}], {})
    assert result['unknown_intensity'] == 1
    assert result['known_intensity'] == 0
    assert result['mean_known_intensity'] is None
    assert result['unknown_reference'] == 1
    assert result['intensity_sources'] == {'unknown': 1}


def test_other_selected_vibes_do_not_supply_a_negative_or_positive_reference():
    result = reports.coverage([{'key': 'labeled', 'artist': 'Someone'}],
                              {'labeled': {'labels': ['relax']}}, 'release')
    assert result['exact_recording_labels'] == 1
    assert result['selected_vibe_positives'] == 0
    assert result['unknown_vibe_reference'] == 1


def test_recording_identity_does_not_establish_a_matching_inference_window():
    tracks = [{'key': key, 'artist': 'Someone'} for key in ('same', 'different')]
    labels = {key: {'labels': ['relax'], 'capture_windows_match': key == 'same'}
              for key in ('same', 'different')}
    result = reports.coverage(tracks, labels, 'relax')
    assert result['exact_recording_labels'] == result['selected_vibe_positives'] == 2
    assert result['exact_capture_window_labels'] == result['selected_vibe_window_positives'] == 1


def test_account_clone_repoints_all_file_owners_and_preserves_the_frozen_source(tmp_path):
    import json
    import sqlite3
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE retained(filename TEXT,payload TEXT)')
        conn.execute("INSERT INTO retained VALUES ('/native/file','unchanged')")
        conn.execute('CREATE TABLE sample_attributes(id TEXT,filename TEXT)')
    (tmp_path / 'files.json').write_text(json.dumps({'columns': [['retained', 'filename']],
        'files': {'/native/file': {'path': '/private/copy', 'sha256': 'checked'}}, 'missing': []}))
    (tmp_path / 'samples.json').write_text('{"rows": [], "missing": []}')
    before = evaluation.digest(source)
    evaluation.clone(tmp_path, tmp_path / 'cloned.db')
    assert evaluation.digest(source) == before
    with sqlite3.connect(tmp_path / 'cloned.db') as conn:
        assert conn.execute('SELECT * FROM retained').fetchone() == ('/private/copy', 'unchanged')


def test_changed_order_is_distinct_from_membership_and_missing_profiles_are_rejected():
    result = reports.output_difference([{'key': 'a'}, {'key': 'b'}], [{'key': 'b'}, {'key': 'a'}])
    assert result['jaccard'] == 1
    assert not result['identical_order']
    assert result['same_positions'] == 0
    with pytest.raises(ValueError, match='different profile'):
        reports.verify_served_profiles([{'key': 'a', 'musical': {'digest': 'old'}}], {'a': {'digest': 'new'}})


def test_legacy_capture_fallback_does_not_hide_missing_specialists_on_compatible_audio():
    with pytest.raises(ValueError, match='Compatible capture'):
        reports.verify_route({}, 'shared', {'relax': 'specialist'}, compatible=True)
    assert reports.verify_route({}, 'shared', {'relax': 'specialist'}, compatible=False) == {}
    prediction = {'model': {'sha256': 'shared'}, 'individual_models': {'relax': {'sha256': 'specialist'}}}
    assert reports.verify_route(prediction, 'shared', {'relax': 'specialist'}, compatible=True) == {'relax': 'specialist'}
    with pytest.raises(ValueError, match='Incompatible capture'):
        reports.verify_route(prediction, 'shared', {'relax': 'specialist'}, compatible=False)
