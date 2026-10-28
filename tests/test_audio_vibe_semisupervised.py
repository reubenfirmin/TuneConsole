"""Unlabeled geometry must not smuggle answers or evaluation songs into fitting."""
from copy import deepcopy
import json
import sqlite3

import numpy as np
import pytest

from experiments.audio_lab_tools import robust_vibes as robust, semisupervised_vibes as semi, train_vibes as trainer
from tests.test_audio_vibe_model import examples, excerpts
from yt_playlist.rec import audio_vibe_model as model, audio_vibe_preview as heuristic


def unlabeled_examples():
    result = []
    for index in range(6):
        row = deepcopy(examples()[0])
        for key in ('labels', 'feedback_run_id', 'updated_at'):
            row.pop(key)
        row.update(video_id=f'unlabeled-{index}', title=f'Unlabeled {index}')
        row['values'][-1] = index / 5
        result.append(row)
    return result


@pytest.mark.parametrize('classes', [1, 6])
def test_manifold_gradient_matches_direct_edge_loss_and_finite_differences(classes):
    rng = np.random.default_rng(507)
    differences = rng.normal(size=(7, 4))
    differences[:, 0] = 0  # Common intercept cancels across an edge.
    offsets = rng.normal(size=(7, classes))
    weights = np.arange(1., 8.); weights /= weights.sum()
    gram = differences.T @ (weights[:, None] * differences)
    linear = differences.T @ (weights[:, None] * offsets)
    constant = (weights[:, None] * offsets ** 2).sum(axis=0)
    coefficients = rng.normal(size=(4, classes))
    flat = coefficients.ravel()
    loss, gradient = trainer.manifold_objective(flat, gram, linear, constant, .3)
    expected = .3 / 2 * np.sum(weights[:, None] * (differences @ coefficients + offsets) ** 2)
    assert loss == pytest.approx(expected)
    assert gradient.reshape(4, classes)[0] == pytest.approx(np.zeros(classes))
    for index in range(len(flat)):
        shift = np.zeros_like(flat); shift[index] = 1e-6
        high = trainer.manifold_objective(flat + shift, gram, linear, constant, .3)[0]
        low = trainer.manifold_objective(flat - shift, gram, linear, constant, .3)[0]
        assert gradient[index] == pytest.approx((high - low) / 2e-6, abs=1e-8)


@pytest.mark.parametrize('objective', ['legacy_binary', 'acceptable_nll'])
def test_unlabeled_geometry_changes_coefficients_without_becoming_targets(objective):
    records, pool = examples(), unlabeled_examples()
    plain = trainer.fit(records, objective=objective)
    assert plain == trainer.fit(records, objective=objective, manifold_weight=0.)
    smoothed = trainer.fit(records, objective=objective, manifold_weight=.1, unlabeled_examples=pool)
    assert plain['center'] == smoothed['center'] and plain['scale'] == smoothed['scale']
    assert plain['classifiers'] != smoothed['classifiers']
    assert smoothed['training']['songs'] == 10
    assert smoothed['training']['manifold']['unlabeled_songs'] == 6
    assert smoothed['training']['manifold']['graph_songs'] == 16
    for classifier in smoothed['classifiers'].values():
        assert classifier['positive_songs'] + classifier['unselected_songs'] == 10
    assert model.validate(json.loads(json.dumps(smoothed))) == smoothed


@pytest.mark.parametrize('mutation', ['overlap', 'duplicate', 'answer', 'saved_prediction', 'signature', 'infinite', 'baseline'])
def test_unlabeled_pool_rejects_answers_overlap_and_incompatible_features(mutation):
    records, pool = examples(), unlabeled_examples()
    if mutation == 'overlap':
        pool[0]['video_id'] = records[-1]['video_id']
    elif mutation == 'duplicate':
        pool.append(pool[0])
    elif mutation == 'answer':
        pool[0]['labels'] = ['cruise']
    elif mutation == 'saved_prediction':
        pool[0]['feedback_prediction'] = {'class': 'agreement'}
    elif mutation == 'signature':
        pool[0]['source_signature'] = 'b' * 64
    elif mutation == 'infinite':
        pool[0]['values'][0] = float('inf')
    else:
        pool[0]['baseline']['relax'] = 2.
    with pytest.raises(ValueError):
        robust.train(records, candidates={'ridge': {'regularization': 1.}}, unlabeled_examples=pool)


def test_outer_song_features_and_answers_cannot_change_graph_or_fitted_model(monkeypatch):
    records, pool = examples()[:6], unlabeled_examples()
    candidates = {'graph_unlabeled_nll_0.1': {'regularization': 1., 'manifold_weight': .1, 'use_unlabeled': True}}
    fitted = []
    predict = robust.predict

    def observe(learned, held_out, training_songs):
        if held_out['video_id'] == records[-1]['video_id'] and training_songs == len(records) - 1:
            fitted.append(deepcopy(learned))
        return predict(learned, held_out, training_songs)

    monkeypatch.setattr(robust, 'predict', observe)
    before = robust.train(records, candidates=candidates, inner_folds=2, unlabeled_examples=pool)[1]
    original = fitted[:]; fitted.clear()
    changed = deepcopy(records)
    changed[-1]['labels'] = ['release']
    changed[-1]['values'] = [1e9] * len(model.FEATURES)
    changed[-1]['subgenre'] = 'detroit techno'
    after = robust.train(changed, candidates=candidates, inner_folds=2, unlabeled_examples=pool)[1]
    assert fitted == original
    assert before['outer_selections'][-1] == after['outer_selections'][-1]
    assert before['inputs']['unlabeled_songs'] == 6


def test_collection_reads_legacy_partial_unlabeled_captures_without_database_writes(store, tmp_path):
    for index in range(6):
        samples = excerpts(f'capture-{index}')
        for sample in samples[:3 if index < 4 else 1]:
            store.audio_vibes.save_sample(sample, index + 1.)
        if index < 4:
            store.audio_vibes.save_feedback(samples[0]['run_id'], [], ['cruise'], heuristic.TAXONOMY_VERSION, None, index + 2.)
    database = tmp_path / 'snapshot.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = database.read_bytes()
    snapshot = semi.collect(database)
    assert database.read_bytes() == before
    assert len(snapshot['examples']) == 4
    assert len(snapshot['unlabeled']) == 2
    for row in snapshot['unlabeled']:
        assert len(row['sample_ids']) == 1
        assert 'labels' not in row and 'feedback_prediction' not in row


@pytest.mark.parametrize('objective', ['legacy_binary', 'acceptable_nll'])
def test_graph_classifier_exports_to_existing_runtime_and_preserves_raw_measurements(objective, tmp_path, monkeypatch):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    records, pool = examples(), unlabeled_examples()
    for row in records + pool:
        row['source_signature'] = model.signature(samples[0])
        row['baseline'] = {entry['vibe']: entry['score'] for entry in baseline['ranking']}
    records[-1]['values'] = model.vector(samples, baseline)
    learned = trainer.fit(records, objective=objective, manifold_weight=.1, unlabeled_examples=pool)
    path = tmp_path / 'graph-model.json'
    path.write_text(json.dumps(learned))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    scores = model.scores(learned, trainer.example_vector(records[-1], learned['features']), records[-1]['baseline'])
    result = heuristic.predict(samples)
    assert {row['vibe']: row['score'] for row in result['ranking']} == scores
    assert result['arc'] == baseline['arc']
    assert result['model']['training_songs'] == 10


def test_cli_freezes_pool_and_comparison_without_activating_candidate(tmp_path, monkeypatch):
    source, output, report = (tmp_path / name for name in ('features.json', 'model.json', 'report.json'))
    snapshot = {'examples': examples()[:4], 'unlabeled': unlabeled_examples()}
    encoded = json.dumps(snapshot); source.write_text(encoded)
    monkeypatch.setattr(semi, 'CANDIDATES', {'ridge': {'regularization': 1.},
        'graph_unlabeled_nll_0.1': {'regularization': 1., 'manifold_weight': .1, 'use_unlabeled': True}})
    monkeypatch.setenv(model.ENVIRONMENT_KEY, '')
    semi.main(['--dataset', str(source), '--output', str(output), '--report', str(report), '--inner-folds', '2'])
    assert source.read_text() == encoded
    assert model.configured_path() is None
    assert model.validate(json.loads(output.read_text()))
    result = json.loads(report.read_text())
    assert result['inputs']['unlabeled_songs'] == 6
    assert result['inputs']['unlabeled_dataset_sha256']
    assert json.loads(report.with_suffix('.dataset.json').read_text()) == snapshot
    assert result['family_comparison']['supervised']['songs'] == 4
    assert result['family_comparison']['unlabeled_graph']['songs'] == 4
    assert {row['video_id'] for row in result['unlabeled_predictions']} == {row['video_id'] for row in snapshot['unlabeled']}
    assert all(row['used_as_training_target'] is False for row in result['unlabeled_predictions'])


def test_similarity_graph_preserves_duplicate_feature_rows_without_self_edges():
    records = examples()
    for row in records:
        row['values'] = [0.] * len(model.FEATURES)
    names, center, scale, _, _ = trainer.training_design(records)
    gram, linear, constant, graph = trainer.manifold_terms(records, [], names, center, scale, 1., 5)
    assert np.isfinite(gram).all() and np.isfinite(linear).all() and np.isfinite(constant).all()
    assert np.count_nonzero(gram) == 0
    assert graph['edges'] > 0
    assert graph['graph_songs'] == 10


@pytest.mark.parametrize('weight', [-1., float('nan'), True])
def test_invalid_manifold_configuration_is_rejected(weight):
    with pytest.raises(ValueError):
        trainer.fit(examples(), manifold_weight=weight)
