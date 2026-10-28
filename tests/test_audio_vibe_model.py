"""Local fitting must not leak songs between folds or rewrite observed evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from experiments.audio_lab_tools import train_vibes as trainer
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog
from yt_playlist.core import features
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import audio_vibe_model as model
from yt_playlist.rec import audio_vibe_preview as heuristic
from yt_playlist.core import audio_vibe_metadata
from yt_playlist.web.app import create_app


def excerpts(video='observed_song'):
    first = sample()
    return [{**sample(run_id=first['run_id'], point=point), 'video_id': video}
            for point in ('thirty', 'middle', 'two_thirds')]


def artifact(samples, intercept=12.):
    return {'schema': model.SCHEMA, 'feature_version': model.FEATURE_VERSION,
            'taxonomy_version': heuristic.TAXONOMY_VERSION, 'features': list(model.FEATURES),
            'threshold': model.THRESHOLD, 'heuristic_version': heuristic.VERSION,
            'source_signature': model.signature(samples[0]), 'center': [0.] * len(model.FEATURES),
            'scale': [1.] * len(model.FEATURES), 'training': {'songs': 12},
            'classifiers': {'energize': {'intercept': intercept, 'weights': [0.] * len(model.FEATURES)}}}


def examples():
    result = []
    for index in range(10):
        high = index >= 5
        values = [0.] * len(model.FEATURES)
        values[-1] = float(high)
        result.append({'video_id': f'song-{index}', 'feedback_run_id': f'run-{index}', 'sample_ids': [],
                       'title': f'Song {index}', 'labels': ['energize' if high else 'cruise'],
                       'values': values, 'source_signature': 'a' * 64, 'updated_at': float(index),
                       'baseline': {key: .6 if key in ('energize', 'cruise') else .1 for key in model.LABELS}})
    return result


def test_grouped_evaluation_fits_only_training_songs_and_selects_fold_specific_support(monkeypatch):
    records = examples()
    fitted = []
    original = trainer.fit

    def observed(training, *args, **kwargs):
        fitted.append({row['video_id'] for row in training})
        return original(training, *args, **kwargs)

    monkeypatch.setattr(trainer, 'fit', observed)
    result = trainer.evaluate(records)
    assert len(fitted) == (len(trainer.CANDIDATES) - 1) * len(records)
    for index, training in enumerate(fitted):
        assert len(training) == 9
        assert records[index % len(records)]['video_id'] not in training
    assert result['ridge_1']['accuracy'] > result['heuristic']['accuracy']
    assert result['ridge_1']['correct'] == 10
    for row in result['ridge_1']['predictions']:
        assert row['trained_labels'] == sorted(model.LABELS)
    with pytest.raises(ValueError, match='one answer per song'):
        trainer.evaluate(records + [records[0]])


def test_too_few_positives_keeps_heuristic_and_training_normalization_excludes_holdout():
    records = examples()
    trained = trainer.fit(records[:7], objective='legacy_binary')
    assert trained['classifiers'] == {}
    assert model.scores(trained, records[-1]['values'], records[-1]['baseline']) == records[-1]['baseline']
    changed = deepcopy(records)
    changed[-1]['values'][-1] = 1e9
    training = trainer.fit(changed[:-1])
    assert training['center'][-1] == pytest.approx(4 / 9)
    assert training['scale'][-1] < 1
    learned, report = trainer.train(records)
    assert learned['training']['songs'] == 10
    assert report['evaluation'] == 'development_leave_one_song_out'
    assert report['selected_candidate'].startswith('ridge_')
    assert model.validate(json.loads(json.dumps(learned))) == learned


def test_readonly_loader_joins_early_answer_without_changing_snapshot_and_deduplicates_songs(store, tmp_path):
    first, repeated, early, partial, cleared = (excerpts(video) for video in
                                               ('same', 'same', 'early', 'partial', 'cleared'))
    for now, (samples, labels, answered_count, available_count) in enumerate((
            (first, ['release'], 3, 3), (repeated, ['energize'], 3, 3),
            (early, ['cruise'], 0, 3), (partial, ['cruise'], 1, 2), (cleared, [], 3, 3)), start=1):
        for row in samples[:available_count]:
            store.audio_vibes.save_sample(row, now)
        store.audio_vibes.save_feedback(samples[0]['run_id'], [s['id'] for s in samples[:answered_count]],
                                        labels, heuristic.TAXONOMY_VERSION, None, now)
    path = tmp_path / 'snapshot.db'
    with sqlite3.connect(path) as destination:
        store.conn.backup(destination)
    before = path.read_bytes()
    records, skipped = trainer.examples_from_db(path)
    assert path.read_bytes() == before
    assert len(records) == 3
    assert records[0]['labels'] == ['energize']
    assert records[0]['feedback_run_id'] == repeated[0]['run_id']
    assert records[1]['feedback_run_id'] == early[0]['run_id']
    assert records[1]['sample_ids'] == [s['id'] for s in early]
    assert records[2]['sample_ids'] == [s['id'] for s in partial[:2]]
    assert store.audio_vibes.feedback(early[0]['run_id'])['sample_ids'] == []
    assert store.audio_vibes.feedback(early[0]['run_id'])['prediction'] is None
    assert skipped == {'earlier_answer_for_same_song': 1, 'empty_or_cleared': 1}
    assert not list(tmp_path.glob('*journal*'))


@pytest.mark.parametrize('count', [1, 2, 3])
@pytest.mark.parametrize('feedback_first', [True, False])
def test_track_feedback_joins_samples_from_independent_playbacks(store, tmp_path, count, feedback_first):
    answer = sample()
    video = answer['video_id']
    store.audio_vibes.save_playback(answer['run_id'], video, 'Song', 'Artist', 1)

    def leave_feedback():
        # Historical sample IDs are not training requirements or joins.
        store.audio_vibes.save_feedback(answer['run_id'], ['historical-sample-no-longer-present'], ['cruise'],
                                        heuristic.TAXONOMY_VERSION, None, 5)

    if feedback_first:
        leave_feedback()
    measurements = [sample(point=point) for point in ('thirty', 'middle', 'two_thirds')[:count]]
    for index, row in enumerate(measurements):
        store.audio_vibes.save_sample(row, index + 10)
    if not feedback_first:
        leave_feedback()
    frozen = store.audio_vibes.feedback(answer['run_id'])
    database = tmp_path / 'track-join.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = database.read_bytes()
    rows, skipped = trainer.examples_from_db(database)
    assert skipped == {}
    assert len(rows) == 1
    row = rows[0]
    assert row['video_id'] == video and row['labels'] == ['cruise']
    assert row['feedback_run_id'] == answer['run_id']
    assert row['sample_ids'] == [s['id'] for s in measurements]
    assert row['sample_run_ids'] == [s['run_id'] for s in measurements]
    assert answer['run_id'] not in row['sample_run_ids']
    baseline = heuristic.predict(measurements, use_model=False, require_complete=False)
    assert baseline['sample_count'] == count
    assert row['values'] == model.vector(measurements, baseline, {})
    if count < 3:
        assert heuristic.predict(measurements) is None, 'Normal popup still waits for its three checks'
    assert database.read_bytes() == before
    assert store.audio_vibes.feedback(answer['run_id']) == frozen


def test_replay_replaces_old_measurements_without_requiring_another_answer(store, tmp_path):
    original = excerpts()
    for row in original:
        store.audio_vibes.save_sample(row, 1)
    store.audio_vibes.save_feedback(original[0]['run_id'], [s['id'] for s in original], ['cruise'],
                                    heuristic.TAXONOMY_VERSION, None, 2)
    frozen = store.audio_vibes.feedback(original[0]['run_id'])
    # An interrupted replay refreshes its captured point; the other observed
    # points remain useful even though they were captured on an earlier play.
    replay = sample(point='thirty', relaxed=.9, aggressive=.1)
    store.audio_vibes.save_sample(replay, 10)
    unrelated = sample()
    unrelated['video_id'] = 'another-track-with-the-same-title'
    store.audio_vibes.save_sample(unrelated, 11)
    database = tmp_path / 'replay.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    rows, skipped = trainer.examples_from_db(database)
    assert skipped == {} and len(rows) == 1
    row = rows[0]
    expected = [replay, *original[1:]]
    assert row['sample_ids'] == [s['id'] for s in expected]
    assert row['sampled_at'] == [10, 1, 1]
    assert row['values'] == model.vector(expected, heuristic.predict(expected, use_model=False), {})
    assert row['values'] != model.vector(original, heuristic.predict(original, use_model=False), {})
    assert store.audio_vibes.feedback(original[0]['run_id']) == frozen
    assert store.audio_vibes.feedback(replay['run_id']) is None


@pytest.mark.parametrize('latest_labels', [['relax'], []])
def test_latest_track_answer_wins_even_when_its_playback_has_no_samples(store, tmp_path, latest_labels):
    samples = excerpts()
    for row in samples:
        store.audio_vibes.save_sample(row, 1)
    store.audio_vibes.save_feedback(samples[0]['run_id'], [], ['cruise'], heuristic.TAXONOMY_VERSION, None, 2)
    latest = sample()
    store.audio_vibes.save_playback(latest['run_id'], latest['video_id'], 'Song', 'Artist', 3)
    store.audio_vibes.save_feedback(latest['run_id'], [], latest_labels, heuristic.TAXONOMY_VERSION, None, 4)
    database = tmp_path / 'latest-answer.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    rows, skipped = trainer.examples_from_db(database)
    assert skipped['earlier_answer_for_same_song'] == 1
    if latest_labels:
        assert len(rows) == 1 and rows[0]['labels'] == latest_labels
        assert rows[0]['sample_ids'] == [s['id'] for s in samples]
        assert rows[0]['feedback_run_id'] == latest['run_id']
    else:
        assert rows == []
        assert skipped['empty_or_cleared'] == 1


def test_runtime_loads_json_correction_and_preserves_raw_arc_and_unsupported_labels(tmp_path, monkeypatch):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(artifact(samples)))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    predicted = heuristic.predict(samples)
    assert predicted['vibe'] == 'energize'
    assert predicted['model']['trained_labels'] == ['energize']
    assert predicted['arc'] == baseline['arc']
    assert predicted['dimensions'] == baseline['dimensions']
    assert predicted['aggregation'] == 'ordered_arc'
    assert predicted['calibrated'] is False
    assert predicted['recommendation_adoption'] is False
    for row in predicted['ranking']:
        previous = next(r for r in baseline['ranking'] if r['vibe'] == row['vibe'])
        if row['vibe'] != 'energize':
            assert row == previous
    assert heuristic.predict(samples, use_model=False) == baseline
    assert heuristic.predict(samples[:2]) is None
    learned = artifact(samples)
    assert model.scores(learned, model.vector(samples, baseline),
                        {r['vibe']: r['score'] for r in baseline['ranking']}) == {
                            r['vibe']: r['score'] for r in predicted['ranking']}


def test_heuristic_winner_artifact_does_not_claim_a_learned_prediction(tmp_path, monkeypatch):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    value = artifact(samples)
    value['classifiers'] = {}
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(value))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    assert heuristic.predict(samples) == baseline


def test_local_configuration_enables_model_and_environment_can_disable_it(tmp_path, monkeypatch):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(artifact(samples)))
    local = tmp_path / '.env.local'
    local.write_text('YT_PLAYLIST_AUDIO_VIBES=1\nYT_PLAYLIST_AUDIO_VIBE_MODEL=' + str(path) + '\n')
    monkeypatch.setattr(features, 'LOCAL_ENV', local)
    monkeypatch.delenv(model.ENVIRONMENT_KEY)
    assert heuristic.predict(samples)['model']['trained_labels'] == ['energize']
    monkeypatch.setenv(model.ENVIRONMENT_KEY, '')
    assert heuristic.predict(samples) == baseline


@pytest.mark.parametrize('mutation', ['missing', 'json', 'taxonomy', 'version', 'features', 'weights',
                                     'nan', 'scale', 'signature', 'attributes', 'oversize'])
def test_missing_invalid_or_incompatible_model_leaves_current_heuristic_intact(tmp_path, monkeypatch, mutation):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    value = artifact(samples)
    path = tmp_path / 'model.json'
    if mutation == 'taxonomy': value['taxonomy_version'] = 'older'
    if mutation == 'version': value['heuristic_version'] = 'older'
    if mutation == 'features': value['features'] = []
    if mutation == 'weights': value['classifiers']['energize']['weights'] = [1.]
    if mutation == 'nan': value['classifiers']['energize']['intercept'] = float('nan')
    if mutation == 'scale': value['scale'][0] = 0
    if mutation == 'signature': value['source_signature'] = 'b' * 64
    if mutation == 'attributes': samples[0]['audio_attributes']['values']['tempo_bpm'] = None
    if mutation != 'missing':
        path.write_text('not json' if mutation == 'json' else json.dumps(value))
    if mutation == 'oversize': path.write_text(' ' * 262145)
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    assert heuristic.predict(samples) == baseline


def test_feedback_freezes_model_identity_when_artifact_is_replaced(store, tmp_path, monkeypatch):
    samples = excerpts()
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(artifact(samples)))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        for row in samples:
            assert client.post('/audio-vibes/samples', json=row).status_code == 200
        payload = {'run_id': samples[0]['run_id'], 'sample_ids': [s['id'] for s in samples],
                   'vibes': ['energize'], 'taxonomy_version': heuristic.TAXONOMY_VERSION}
        saved = client.post('/audio-vibes/feedback', json=payload).json()['feedback']['prediction']
        assert saved['vibe'] == 'energize'
        path.write_text(json.dumps(artifact(samples, -12.)))
        state = client.get('/audio-vibes/runs/' + samples[0]['run_id']).json()
        assert state['prediction']['model']['sha256'] != saved['model']['sha256']
        assert state['feedback']['prediction'] == saved
        assert client.post('/audio-vibes/feedback', json={**payload, 'vibes': ['cruise']}).json()[
            'feedback']['prediction'] == saved


def test_cli_refuses_to_overwrite_database_or_report(tmp_path):
    path = tmp_path / 'state.db'
    with pytest.raises(SystemExit):
        trainer.main(['--db', str(path), '--output', str(path), '--report', str(tmp_path / 'report.json')])
    assert not path.exists()


def test_cli_trains_while_pytest_holds_the_shared_machine_lock(store, tmp_path):
    for index, label in enumerate(('cruise', 'release')):
        samples = excerpts(f'cli-song-{index}')
        for row in samples:
            store.audio_vibes.save_sample(row, index + 1)
        store.audio_vibes.save_feedback(samples[0]['run_id'], [row['id'] for row in samples], [label],
                                        heuristic.TAXONOMY_VERSION, None, index + 1)
    database = tmp_path / 'training.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = database.read_bytes()
    output, report = tmp_path / 'model.json', tmp_path / 'report.json'
    # Pytest owns the shared machine lock for this entire test. The CLI must
    # finish independently, rather than waiting for its own parent to release it.
    result = subprocess.run([sys.executable, '-m', 'experiments.audio_lab_tools.train_vibes',
        '--db', str(database), '--output', str(output), '--report', str(report)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['training_songs'] == 2
    assert model.validate(json.loads(output.read_text()))['training']['songs'] == 2
    assert len(json.loads(report.read_text())['manifest']) == 2
    assert database.read_bytes() == before


def test_base_metadata_can_improve_identical_audio_without_a_source_discount():
    records = examples()
    index = model.FEATURES.index('base.bpm')
    for i, row in enumerate(records):
        row['values'] = [0.] * len(model.FEATURES)
        row['values'][index] = 90. if i < 5 else 160.
    combined = trainer.evaluate(records)
    audio = trainer.evaluate(records, feature_set='audio_only')
    assert combined['ridge_1']['correct'] == 10
    assert combined['ridge_1']['accuracy'] > audio['ridge_1']['accuracy']
    learned = trainer.fit(records)
    moved = deepcopy(records)
    for row in moved:
        row['values'][0], row['values'][index] = row['values'][index], row['values'][0]
    swapped = trainer.fit(moved)
    for key, classifier in learned['classifiers'].items():
        assert classifier['weights'][index] == pytest.approx(swapped['classifiers'][key]['weights'][0])
    _, report = trainer.train(records)
    assert report['input_comparison']['accuracy_change'] > 0
    assert report['metadata_coverage']['observed_numeric_fields']['bpm'] == 10


def test_missing_metadata_imputes_only_training_means_and_preserves_observed_zero():
    records = examples()
    value_index = model.FEATURES.index('base.energy')
    present_index = model.FEATURES.index('base.present.energy')
    for i, row in enumerate(records):
        row['values'][value_index] = None if i < 5 else 0.
        row['values'][present_index] = float(i >= 5)
    learned = trainer.fit(records)
    assert learned['center'][value_index] == 0.
    assert learned['scale'][value_index] == 1.
    assert learned['center'][present_index] == .5
    missing = model.scores(learned, records[0]['values'], records[0]['baseline'])
    measured_zero = model.scores(learned, records[-1]['values'], records[-1]['baseline'])
    assert missing != measured_zero
    # A held-out extreme never becomes an imputation value for its own fold.
    records[-1]['values'][value_index] = 1e9
    assert trainer.fit(records[:-1])['center'][value_index] == 0.
    absent = dict(zip(audio_vibe_metadata.FEATURES, audio_vibe_metadata.vector(), strict=True))
    zero = dict(zip(audio_vibe_metadata.FEATURES, audio_vibe_metadata.vector({'energy': 0}), strict=True))
    assert absent['base.energy'] is None and absent['base.present.energy'] == 0
    assert zero['base.energy'] == 0 and zero['base.present.energy'] == 1


def test_exact_catalog_metadata_joins_readonly_training_and_runtime_and_freezes_answer(store, tmp_path, monkeypatch):
    samples = excerpts()
    tid = store.tracks.upsert_track(samples[0]['video_id'], 'Song', 'Artist', '', 240)
    with store.conn:
        for field, value in {'bpm': 170., 'energy': 0., 'music_key': 'Bb', 'music_scale': 'Minor',
                             'genre': 'Ambient', 'mb_year': '2001'}.items():
            catalog.choose_field(store.conn, tid, field, value)
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [MetadataObservation(
        'mood_aggressive', .8, 'recording', 'recording-mbid', 'prediction',
        match_evidence={'method': 'supplied_recording_mbid'})], run_id='provider', now=1)
    metadata = store.audio_vibes.base_metadata(samples[0]['video_id'])
    assert metadata['bpm'] == 170. and metadata['energy'] == 0.
    assert metadata['mood_aggressive'] == .8
    assert store.audio_vibes.base_metadata('not-the-same-video') == {}
    values = model.vector(samples, heuristic.predict(samples, use_model=False), metadata)
    assert values[model.FEATURES.index('base.bpm')] == 170.
    assert values[model.FEATURES.index('base.mood_aggressive')] == .8
    assert values[model.FEATURES.index('base.key.A#')] == 1.
    assert values[model.FEATURES.index('base.scale.minor')] == 1.
    assert values[model.FEATURES.index('base.year')] == 2001
    for row in samples:
        store.audio_vibes.save_sample(row, 2)
    store.audio_vibes.save_feedback(samples[0]['run_id'], [s['id'] for s in samples], ['energize'],
                                    heuristic.TAXONOMY_VERSION, None, 3)
    database = tmp_path / 'snapshot.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = database.read_bytes()
    rows, _ = trainer.examples_from_db(database)
    assert database.read_bytes() == before
    assert rows[0]['values'] == values
    # A nonzero coefficient proves API inference actually consumes catalog values.
    learned = artifact(samples, intercept=-12.)
    learned['classifiers']['energize']['weights'][model.FEATURES.index('base.bpm')] = .1
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(learned))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        predicted = client.get('/audio-vibes/runs/' + samples[0]['run_id']).json()['prediction']
        assert next(row for row in predicted['ranking'] if row['vibe'] == 'energize')['score'] > .99
        # Clear the fixture annotation so the API captures a fresh hypothesis.
        with store.conn:
            store.conn.execute('DELETE FROM audio_session_labels')
        payload = {'run_id': samples[0]['run_id'], 'sample_ids': [s['id'] for s in samples],
                   'vibes': ['energize'], 'taxonomy_version': heuristic.TAXONOMY_VERSION}
        saved = client.post('/audio-vibes/feedback', json=payload).json()['feedback']['prediction']
        assert saved == predicted
        with store.conn:
            catalog.choose_field(store.conn, tid, 'bpm', 60.)
            # Native aggression has no legacy catalog column/editor field. Its
            # retained choice still blocks fallback in the shared input reader.
            store.conn.execute("INSERT INTO entry_field_choices VALUES (?,'mood_aggressive','null','fixture')", (tid,))
        updated = client.get('/audio-vibes/runs/' + samples[0]['run_id']).json()
        assert next(row for row in updated['prediction']['ranking'] if row['vibe'] == 'energize')['score'] < .1
        assert updated['feedback']['prediction'] == saved
        assert store.audio_vibes.base_metadata(samples[0]['video_id'])['mood_aggressive'] is None


def test_enabled_legacy_artifact_keeps_working_while_metadata_candidate_is_evaluated(tmp_path, monkeypatch):
    samples = excerpts()
    learned = artifact(samples)
    learned['feature_version'] = model.LEGACY_FEATURE_VERSION
    learned['features'] = list(model.AUDIO_FEATURES)
    learned['center'] = [0.] * len(model.AUDIO_FEATURES)
    learned['scale'] = [1.] * len(model.AUDIO_FEATURES)
    learned['classifiers']['energize']['weights'] = [0.] * len(model.AUDIO_FEATURES)
    path = tmp_path / 'legacy-model.json'
    path.write_text(json.dumps(learned))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    predicted = heuristic.predict(samples, metadata={'bpm': 80.})
    assert predicted['vibe'] == 'energize'
    assert predicted['model']['feature_version'] == model.LEGACY_FEATURE_VERSION


@pytest.mark.parametrize('genre', ['Electronic', 'Rock', 'Techno', 'House', 'Pop', 'Jazz', 'Ambient', 'hiphop'])
def test_broad_genres_have_no_feature_or_presence_reward(genre):
    assert audio_vibe_metadata.subgenre({'genre': genre}) is None
    assert audio_vibe_metadata.vector({'genre': genre}, subgenres=('detroit techno', 'deep house')) == (
        audio_vibe_metadata.vector({}, subgenres=('detroit techno', 'deep house')))
    assert not any('base.family.' in name or name == 'base.present.genre' for name in model.FEATURES)


def test_exact_subgenre_features_have_no_parent_votes_and_vocabulary_is_fold_local():
    records = examples()
    for i, row in enumerate(records):
        row['subgenre'] = 'detroit techno' if i < 5 else 'deep house'
    # The held-out genre must not create a column in the training fold.
    records[-1]['subgenre'] = 'acid techno'
    learned = trainer.fit(records[:-1])
    assert audio_vibe_metadata.vocabulary(learned['features']) == ('deep house', 'detroit techno')
    assert model.validate(learned) == learned
    specific = audio_vibe_metadata.vector({'genre': 'Detroit Techno'}, subgenres=('deep house', 'detroit techno'))
    sibling = audio_vibe_metadata.vector({'genre': 'Acid Techno'}, subgenres=('deep house', 'detroit techno'))
    assert specific[-2:] == [0, 1]
    assert sibling[-2:] == [0, 0]
    assert trainer.example_vector(records[0], learned['features'])[-2:] == [0, 1]
    assert trainer.example_vector(records[-1], learned['features'])[-2:] == [0, 0]
    # Inference shares the exact exported vocabulary, not a fresh genre lookup
    # vocabulary that could change dimensionality or conflate sibling styles.
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    full = model.vector(samples, baseline, {'genre': 'Detroit Techno'}, feature_names=learned['features'])
    assert len(full) == len(learned['features'])
    assert full[-2:] == [0, 1]
    invalid = deepcopy(learned)
    invalid['features'][-1] = 'base.subgenre.electronic'
    with pytest.raises(ValueError, match='subgenre vocabulary'):
        model.validate(invalid)
