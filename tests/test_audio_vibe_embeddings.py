"""Frozen embeddings cross the capture contract and train without label leakage."""
from copy import deepcopy
import json
import sqlite3

from fastapi.testclient import TestClient
import numpy as np
import pytest

from experiments.audio_lab_tools import robust_vibes, train_vibes as trainer
from tests.test_audio_vibe_model import examples, excerpts
from tests.test_audio_vibes import sample
from yt_playlist.core import audio_embedding, sample_files
from yt_playlist.rec import audio_vibe_model as model, audio_vibe_preview as heuristic
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.audio_vibes import Sample


def with_embedding(value, number=-.25):
    return {**value, 'embedding': {'version': audio_embedding.VERSION, 'layer': audio_embedding.LAYER,
        'graph_policy': audio_embedding.GRAPH_POLICY, 'pooling': audio_embedding.POOLING,
        'patches': 3, 'values': [number] * 200},
        'artifact_hashes': {**value['artifact_hashes'], 'musicnn/group1-shard1of1.bin': 'b' * 64}}


def embedding_examples():
    records = examples()
    for i, row in enumerate(records):
        row['values'] = [0.] * len(model.FEATURES)
        row['embedding_values'] = [float(i >= 5) * 2 - 1] * 200
        row['embedding_signature'] = audio_embedding.signature(with_embedding(sample()))
    return records


def test_embedding_is_durable_and_legacy_retry_keeps_content_address(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    legacy = Sample.model_validate_json(json.dumps(sample())).model_dump(mode='json')
    legacy.pop('embedding')
    store.audio_vibes.save_sample(legacy, 1.)
    value = with_embedding(sample(point='middle', run_id=legacy['run_id']))
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        assert client.post('/audio-vibes/samples', json=legacy).status_code == 200
        response = client.post('/audio-vibes/samples', json=value)
        assert response.status_code == 200, response.text
        assert client.post('/audio-vibes/samples', json=value).status_code == 200
    saved = store.audio_vibes.samples(legacy['run_id'])
    assert saved[1]['embedding'] == value['embedding']
    assert 'embedding' not in saved[0]
    filename = store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?', (value['id'],)).fetchone()[0]
    assert sample_files.read(filename)['embedding']['values'] == [-.25] * 200


@pytest.mark.parametrize('mutation', ['length', 'nonfinite', 'boolean', 'pooling', 'layer', 'extra',
                                     'patches', 'provenance', 'model', 'trait_coverage'])
def test_api_rejects_incompatible_embedding_contract(store, monkeypatch, mutation):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    value = with_embedding(sample())
    embedding = value['embedding']
    if mutation == 'length':
        embedding['values'].pop()
    elif mutation == 'nonfinite':
        embedding['values'][0] = float('inf')
    elif mutation == 'boolean':
        embedding['values'][0] = True
    elif mutation in ('pooling', 'layer'):
        embedding[mutation] = 'unknown'
    elif mutation == 'extra':
        embedding['pcm'] = [1, 2]
    elif mutation == 'patches':
        embedding['patches'] = 2
    elif mutation == 'provenance':
        value['artifact_hashes'].pop('musicnn/group1-shard1of1.bin')
    elif mutation == 'model':
        value['model'] = 'different-model'
    else:
        value['traits']['relaxed']['per_patch'] = [value['traits']['relaxed']['score']]
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        response = client.post('/audio-vibes/samples', content=json.dumps(value),
                               headers={'content-type': 'application/json'})
        assert response.status_code == 422
    assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 0


def test_partial_embeddings_join_independent_feedback_and_skip_newer_legacy_captures(store, tmp_path):
    answer = sample()
    store.audio_vibes.save_playback(answer['run_id'], answer['video_id'], 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback(answer['run_id'], [], ['cruise', 'concentrate'], heuristic.TAXONOMY_VERSION, None, 2)
    older = sample()
    measured = with_embedding(sample(), -.5)
    newer = sample()
    for value, now in ((older, 3), (measured, 4), (newer, 5)):
        store.audio_vibes.save_sample(value, now)
    database = tmp_path / 'embedding-snapshot.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = database.read_bytes()
    standard, _ = trainer.examples_from_db(database)
    records, skipped = trainer.examples_from_db(database, require_embeddings=True)
    assert database.read_bytes() == before
    assert standard[0]['sample_ids'] == [newer['id']]
    assert 'embedding_values' not in standard[0]
    assert records[0]['sample_ids'] == [measured['id']]
    assert records[0]['feedback_run_id'] == answer['run_id'] != measured['run_id']
    assert records[0]['embedding_values'] == [-.5] * 200
    assert records[0]['labels'] == ['cruise', 'concentrate']
    # Once a compatible point is found, still older copies need not be inspected.
    assert skipped['missing_or_invalid_embedding'] == 1


@pytest.mark.parametrize('feature_set', ['embeddings_only', 'embeddings_combined'])
def test_json_export_uses_frozen_features_and_matches_runtime(feature_set, tmp_path, monkeypatch):
    records = embedding_examples()
    samples = [with_embedding(value, 1.) for value in excerpts()]
    baseline = heuristic.predict(samples, use_model=False)
    for row in records:
        row['source_signature'] = model.signature(samples[0])
        row['baseline'] = {entry['vibe']: entry['score'] for entry in baseline['ranking']}
    learned = trainer.fit(records, feature_set=feature_set, baseline_weight=0.)
    assert learned['feature_version'] == model.EMBEDDING_FEATURE_VERSION
    assert model.validate(json.loads(json.dumps(learned))) == learned
    expected = trainer.example_vector({**records[-1], 'values': model.vector(samples, baseline)}, learned['features'])
    values = model.vector(samples, baseline, feature_version=learned['feature_version'], feature_names=learned['features'])
    assert values == expected
    path = tmp_path / 'embedding-model.json'
    path.write_text(json.dumps(learned))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    result = heuristic.predict(samples)
    assert result['vibe'] == 'energize'
    assert result['vibes'] == ['energize']
    assert result['arc'] == baseline['arc']
    assert result['model']['feature_version'] == model.EMBEDDING_FEATURE_VERSION
    missing = deepcopy(samples)
    missing[0].pop('embedding')
    assert 'model' not in heuristic.predict(missing)
    changed = deepcopy(samples)
    changed[0]['artifact_hashes']['musicnn/group1-shard1of1.bin'] = 'c' * 64
    assert 'model' not in heuristic.predict(changed)


def test_nested_embedding_comparison_uses_identical_tracks_and_training_fold_statistics():
    records = embedding_examples()
    _, _, _, design, _ = trainer.training_design(records[:-1], 'embeddings_only')
    changed = deepcopy(records)
    changed[-1]['embedding_values'] = [1e6] * 200
    assert np.array_equal(design, trainer.training_design(changed[:-1], 'embeddings_only')[3])
    candidates = {'attributes': {'regularization': 1.}, 'embeddings': {
        'regularization': 1., 'baseline_weight': 0., 'feature_set': 'embeddings_only'}}
    learned, report = robust_vibes.train(records, candidates=candidates, inner_folds=2)
    assert learned['training']['songs'] == len(records)
    assert report['inputs']['embedding_capable_songs'] == len(records)
    for result in report['development_results'].values():
        assert {row['video_id'] for row in result['predictions']} == {row['video_id'] for row in records}
    assert report['development_results']['embeddings']['accuracy'] == 1.
    records[0]['embedding_signature'] = 'c' * 64
    with pytest.raises(ValueError, match='compatible frozen embedding version'):
        robust_vibes.train(records, candidates=candidates, inner_folds=2)


def test_excerpt_pooling_preserves_zero_negative_values_and_rejects_mixed_provenance():
    samples = [with_embedding(sample(), value) for value in (-1., 0., 2.)]
    assert audio_embedding.vector(samples) == pytest.approx([1 / 3] * 200)
    samples[-1]['artifact_hashes']['musicnn/group1-shard1of1.bin'] = 'c' * 64
    with pytest.raises(ValueError, match='incompatible'):
        audio_embedding.vector(samples)


def test_selection_does_not_compare_sigmoid_and_softmax_confidence_gaps():
    sigmoid = {'accuracy': .6, 'disagreement_classes': {'wide_divergence': 5}}
    softmax = {'accuracy': .6, 'disagreement_classes': {'wide_divergence': 1}}
    assert robust_vibes.selection_key(sigmoid) == robust_vibes.selection_key(softmax)


@pytest.mark.parametrize('tracks', [4, 5, 10])
def test_embedding_cli_exports_replayable_features_without_activating_model(tmp_path, monkeypatch, tracks):
    source, output, report_path = (tmp_path / name for name in ('features.json', 'model.json', 'report.json'))
    content = json.dumps({'examples': embedding_examples()[:tracks], 'skipped': {'missing_embedding': 3}})
    source.write_text(content)
    monkeypatch.setattr(robust_vibes, 'EMBEDDING_CANDIDATES', {'embeddings': {
        'regularization': 1., 'baseline_weight': 0., 'feature_set': 'embeddings_only'}})
    monkeypatch.setenv(model.ENVIRONMENT_KEY, '')
    robust_vibes.main(['--dataset', str(source), '--embeddings',
                      '--output', str(output), '--report', str(report_path)])
    assert source.read_text() == content
    assert model.configured_path() is None
    learned = model.validate(json.loads(output.read_text()))
    report = json.loads(report_path.read_text())
    snapshot = json.loads(report_path.with_suffix('.dataset.json').read_text())
    assert report['inputs']['embedding_capable_songs'] == tracks
    assert report['inner_folds'] == min(5, tracks - 1)
    assert report['skipped'] == {'missing_embedding': 3}
    assert snapshot == json.loads(content)
    assert learned['feature_version'] == model.EMBEDDING_FEATURE_VERSION
