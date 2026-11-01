"""Identity and listener-count checks for newly acquired external annotations."""
import csv
import json

import pytest

from experiments.audio_lab_tools import external_emotify_plus as plus
from experiments.audio_lab_tools.external_experiment import digest, save
from experiments.audio_lab_tools.external_training import feature_matrices


def prepare_fixture(tmp_path, monkeypatch, annotations):
    data, out = tmp_path / 'data', tmp_path / 'out'
    (data / 'emotify-plus').mkdir(parents=True)
    out.mkdir()
    path = data / 'emotify-plus/musicemotions_dataset_v2.csv'
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['fileName', 'songID', 'Genre', 'SongName',
                                                   'UserID', 'Rating', 'Feeling'])
        writer.writeheader()
        writer.writerows(annotations)
    save(out / 'emotify-artist.features.json', {'rows': [
        {'id': 'emotify:201', 'audio': '/audio/electronic/1.mp3', 'title': '東京',
         'artist': 'Example artist', 'group': 'artist:example', 'targets': [0.]},
        {'id': 'emotify:301', 'audio': '/audio/pop/1.mp3', 'title': 'Other title',
         'artist': 'Another artist', 'group': 'artist:another', 'targets': [0.]}]})
    monkeypatch.setattr(plus, 'DATA', data)
    monkeypatch.setattr(plus, 'OUT', out)
    monkeypatch.setattr(plus, 'SHA256', digest(path))
    return out


def row(**changes):
    return {'fileName': 'electronic/1.mp3', 'songID': '999', 'Genre': 'ELECTRIC',
            'SongName': '東京', 'UserID': '1', 'Rating': '0', 'Feeling': 'relaxing', **changes}


def test_listener_votes_are_deduplicated_and_missing_intensity_stays_missing(tmp_path, monkeypatch):
    out = prepare_fixture(tmp_path, monkeypatch, [row(), row(Rating='4'),
        row(UserID='2', Feeling='happy', Rating='5')])
    audit = plus.prepare()
    observed = json.loads((out / 'emotify-plus-strict.features.json').read_text())['rows'][0]
    assert observed['id'] == 'emotify:201'  # Native song IDs do not identify Emotify files.
    assert observed['group'] == 'artist:example'
    assert observed['rater_count'] == 2
    assert observed['targets'][plus.TARGETS.index('relaxing')] == .5
    assert observed['conditional_intensity_means_0_to_5']['relaxing'] == 2
    assert observed['conditional_intensity_means_0_to_5']['sad'] is None
    assert observed['emotion_listener_counts']['sad'] == 0
    assert audit['repeated_listener_track_emotion_rows'] == 1


def test_conflicting_release_title_excludes_complete_track(tmp_path, monkeypatch):
    out = prepare_fixture(tmp_path, monkeypatch, [row(), row(UserID='2', SongName='京都'),
        row(fileName='pop/1.mp3', songID='1', Genre='POP', SongName='Other title')])
    audit = plus.prepare()
    retained = json.loads((out / 'emotify-plus-strict.features.json').read_text())['rows']
    assert [r['id'] for r in retained] == ['emotify:301']
    assert audit['excluded_audio'] == ['electronic/1.mp3']


def test_shared_native_song_id_cannot_join_different_files(tmp_path, monkeypatch):
    prepare_fixture(tmp_path, monkeypatch, [row(),
        row(fileName='pop/1.mp3', Genre='POP', SongName='Other title')])
    with pytest.raises(ValueError, match='refers to different files'):
        plus.prepare()


def test_conflicting_genre_excludes_complete_track(tmp_path, monkeypatch):
    out = prepare_fixture(tmp_path, monkeypatch, [row(), row(UserID='2', Genre='POP')])
    audit = plus.prepare()
    assert audit['strict_audio_joins'] == 0
    assert json.loads((out / 'emotify-plus-strict.features.json').read_text())['rows'] == []


def test_deam_trained_head_is_excluded_from_every_deam_feature_family(tmp_path):
    path = tmp_path / 'features.json'
    save(path, {'audio_attributes': {'values': {'tempo': 100}}, 'prediction': {
        'embedding': {'values': [1., 2.]}, 'moods': {'traits': {
            'relaxed': {'score': .4, 'range': [0, 1], 'model': 'relaxed'},
            'valence': {'score': 7., 'range': [1, 9], 'model': 'deam'},
            'arousal': {'score': 5., 'range': [1, 9], 'model': 'deam'}}}}})
    families, names, moods, retained = feature_matrices([
        {'id': 'deam:42', 'feature_file': str(path)}])
    assert retained == ['relaxed']
    assert moods.tolist() == [[.4]]
    assert families['attributes_embedding_moods'].shape == (1, 4)
    assert names['attributes_embedding_moods'] == ['tempo', 'musicnn_000', 'musicnn_001', 'relaxed']
    # Preserve explicitly out-of-corpus controls, including their feature names.
    assert feature_matrices([{'id': 'other:42', 'feature_file': str(path)}])[3] == [
        'arousal', 'relaxed', 'valence']


def test_reserved_identity_and_every_pmemo_artist_credit_are_protected():
    from experiments.audio_lab_tools.external_transfer import excluded_identities
    protected = [{'artist': 'Metro Boomin', 'title': '東京'},
                 {'artist': 'Björk', 'title': 'A reserved title'}]
    rows = [
        {'id': 'collaboration', 'artist': '21 Savage+Metro Boomin+Future', 'title': 'Different'},
        {'id': 'unicode', 'artist': 'BJO\u0308RK', 'title': 'Other'},
        {'id': 'title', 'artist': 'Unknown', 'title': '東 京'},
        {'id': 'safe', 'artist': 'Other band', 'title': 'Other song'}]
    assert [row['id'] for row in excluded_identities(rows, protected)] == [
        'collaboration', 'unicode', 'title']


def test_cross_corpus_rejects_overlap_with_secondary_artist_credit():
    from experiments.audio_lab_tools.external_cross_corpus import exclude_overlap
    target = [{'artist': 'Future', 'title': 'Held-out song', 'audio_sha256': 'a'}]
    source = [{'artist': '21 Savage+Metro Boomin+Future', 'title': 'Other', 'audio_sha256': 'b'},
              {'artist': 'Different artist', 'title': 'Different', 'audio_sha256': 'c'}]
    assert exclude_overlap(source, target).tolist() == [False, True]


def test_native_folds_bind_transitive_artist_credits_and_audio_versions():
    from experiments.audio_lab_tools.external_training import validation_groups
    rows = [
        {'id': '1', 'group': 'a', 'artist': 'First+Second', 'audio_sha256': 'x'},
        {'id': '2', 'group': 'b', 'artist': 'Second+Third', 'audio_sha256': 'y'},
        {'id': '3', 'group': 'c', 'artist': 'Third', 'audio_sha256': 'z'},
        {'id': '4', 'group': 'd', 'artist': 'Uncredited artist', 'audio_sha256': 'z'},
        {'id': '5', 'group': 'e', 'artist': '', 'audio_sha256': 'q'}]
    groups = validation_groups(rows)
    assert len(set(groups[:4])) == 1
    assert groups[4] != groups[0]


def test_crossfit_cache_keys_targets_and_rejects_damaged_predictions(tmp_path, monkeypatch):
    import numpy as np
    from experiments.audio_lab_tools import external_prediction_cache as cache
    monkeypatch.setattr(cache, 'OUT', tmp_path)
    features, y = {'native': np.zeros((2, 3))}, np.zeros((2, 2))
    calls = []
    def compute():
        calls.append(True)
        return np.full((2, 1, 2), .5)
    cache.crossfit('test', features, y, ['a', 'b'], compute)
    cache.crossfit('test', features, y, ['a', 'b'], compute)
    assert len(calls) == 1
    cache.crossfit('test', features, y + 1, ['a', 'b'], compute)
    assert len(calls) == 2
    path = next((tmp_path / 'crossfit-cache').glob('*.json'))
    entry = json.loads(path.read_text())
    entry['predictions'][0][0][0] = .9
    save(path, entry)
    with pytest.raises(ValueError, match='integrity mismatch'):
        cache.crossfit('test', features, y if entry['provenance']['targets'] == cache.array_digest(y)
                       else y + 1, ['a', 'b'], compute)
