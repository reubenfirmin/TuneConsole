"""Robust fitting must correct known answers without leaking evaluation targets."""
from copy import deepcopy
import json

import numpy as np
import pytest
from scipy.special import expit

from experiments.audio_lab_tools import robust_vibes as robust
from experiments.audio_lab_tools import train_vibes as trainer
from tests.test_audio_vibe_model import examples
from yt_playlist.rec import audio_vibe_model as model


@pytest.mark.parametrize('radius', [0., .5, 1.])
def test_robust_loss_gradient_and_exact_worst_case(radius):
    design = np.array([[1., .2, 0., 1.], [1., -1., .4, 0.], [1., .8, -.3, 1.]])
    weights = np.array([.1, -.6, .3, .2])
    targets = np.array([0., 1., 1.])
    offset = np.array([.5, -.8, .2])
    importance = np.array([.8, 1.4, .8])
    # Intercept, missing second feature of row 0, and categorical column 3
    # cannot be changed by the adversary.
    mask = np.array([[0., 1., 0., 0.], [0., 1., 1., 0.], [0., 1., 1., 0.]])

    def objective(value):
        return trainer.logistic_objective(value, design, offset, targets, 2.,
            sample_weights=importance, attack_mask=mask, adversarial_radius=radius)

    loss, gradient = objective(weights)
    finite_difference = []
    for i in range(len(weights)):
        shift = np.zeros(len(weights))
        shift[i] = 1e-6
        finite_difference.append((objective(weights + shift)[0] - objective(weights - shift)[0]) / 2e-6)
    assert gradient == pytest.approx(finite_difference, abs=1e-7)
    signed = 2 * targets - 1
    attacked = mask * weights
    norm = np.sqrt((attacked ** 2).sum(axis=1))
    perturbation = -signed[:, None] * radius * attacked / norm[:, None]
    adversarial_logits = offset + (design + perturbation) @ weights
    expected = importance @ np.logaddexp(0, -signed * adversarial_logits) + weights @ weights
    assert expected <= loss <= expected + len(targets) * radius * trainer.ADVERSARIAL_SMOOTHING + 1e-12
    assert np.linalg.norm(perturbation, axis=1) == pytest.approx([radius] * 3)


def test_attack_preserves_missing_and_categorical_features(monkeypatch):
    records = examples()
    continuous = model.FEATURES.index('audio.tempo_bpm')
    missing = model.FEATURES.index('base.bpm')
    for i, row in enumerate(records):
        row['values'][continuous] = 80. + i
        row['values'][missing] = None if i == 0 else 100. + i
    calls = []
    original = trainer.logistic_objective

    def observe(*args, **kwargs):
        calls.append(kwargs['attack_mask'])
        return original(*args, **kwargs)

    monkeypatch.setattr(trainer, 'logistic_objective', observe)
    learned = trainer.fit(records, adversarial_radius=.5, hard_example_weight=2., objective='legacy_binary')
    assert calls
    for mask in calls:
        assert not mask[:, 0].any()
        assert mask[:, 1 + continuous].all()
        assert not mask[0, 1 + missing]
        assert mask[1:, 1 + missing].all()
        assert not mask[:, -1].any()  # metadata presence identity
    assert model.validate(json.loads(json.dumps(learned))) == learned
    assert learned['training']['adversarial_radius'] == .5


def test_nested_selection_never_sees_outer_answer_and_final_fit_keeps_contradictions(monkeypatch):
    records = examples()
    records[0]['feedback_prediction'] = {'class': 'wide_divergence'}
    candidates = {'ridge': {'regularization': 1.}, 'adversarial': {
        'regularization': 1., 'adversarial_radius': .5, 'objective': 'legacy_binary'}}
    original_select, original_fit = robust.select_candidate, trainer.fit
    outer_training = None
    final_fit_ids = []
    selection_calls = []
    all_ids = {row['video_id'] for row in records}
    evaluation_ids = sorted(all_ids)

    def select(training, *args):
        nonlocal outer_training
        outer_training = {row['video_id'] for row in training}
        index = len(selection_calls)
        expected = all_ids - {evaluation_ids[index]} if index < len(records) else all_ids
        assert outer_training == expected
        selection_calls.append(outer_training)
        return original_select(training, *args)

    def fit(training, *args, **kwargs):
        ids = {row['video_id'] for row in training}
        assert ids <= outer_training
        final_fit_ids.append(ids)
        return original_fit(training, *args, **kwargs)

    monkeypatch.setattr(robust, 'select_candidate', select)
    monkeypatch.setattr(trainer, 'fit', fit)
    learned, report = robust.train(records, candidates=candidates, inner_folds=2)
    assert final_fit_ids[-1] == {row['video_id'] for row in records}
    assert learned['training']['songs'] == len(records)
    assert report['evaluation'] == 'nested_leave_one_song_out'
    assert report['nested']['songs'] == len(records)
    assert len(report['outer_selections']) == len(records)
    assert report['known_wide_divergence']['songs'] == 1
    known = report['known_wide_divergence']['corrected_training_fit']
    assert known['correct'] == 1
    assert known['predictions'][0]['video_id'] == records[0]['video_id']
    assert report['training_fit']['correct'] == len(records)
    assert report['dataset_sha256'] == learned['training']['dataset_sha256']


def test_outer_fits_are_unchanged_by_held_out_answers_or_feature_statistics(monkeypatch):
    records = examples()
    candidates = {'heuristic': None, 'ridge': {'regularization': 1.}}
    fitted = []
    original_predict = robust.predict

    def observe(learned, held_out, training_songs):
        if held_out['video_id'] == records[-1]['video_id'] and training_songs == len(records) - 1:
            fitted.append(deepcopy(learned))
        return original_predict(learned, held_out, training_songs)

    monkeypatch.setattr(robust, 'predict', observe)
    original = robust.train(records, candidates=candidates, inner_folds=2)[1]
    original_fits = fitted[:]
    fitted.clear()
    changed = deepcopy(records)
    changed[-1]['labels'] = ['release']
    changed[-1]['values'] = [1e9] * len(model.FEATURES)
    changed[-1]['subgenre'] = 'detroit techno'
    changed[-1]['feedback_prediction'] = {'class': 'wide_divergence'}
    updated = robust.train(changed, candidates=candidates, inner_folds=2)[1]
    # Held-out features legitimately change inference, but neither their extreme
    # values nor the answer can change fitting, normalization or model selection.
    before = original['nested']['predictions'][-1]
    after = updated['nested']['predictions'][-1]
    assert before['video_id'] == after['video_id'] == records[-1]['video_id']
    assert before['human'] != after['human']
    assert fitted == original_fits
    assert before['selected_candidate'] == after['selected_candidate']
    assert original['outer_selections'][-1] == updated['outer_selections'][-1]


def test_hard_example_weights_use_training_errors_and_preserve_total_weight(monkeypatch):
    records = examples()
    original = trainer.logistic_objective
    seen = []

    def observe(weights, design, offset, targets, regularization, **kwargs):
        importance = kwargs['sample_weights']
        expected = 1 + 2 * expit(-(2 * targets - 1) * offset)
        assert importance == pytest.approx(expected / expected.mean())
        assert importance.sum() == pytest.approx(len(records))
        seen.append(True)
        return original(weights, design, offset, targets, regularization, **kwargs)

    monkeypatch.setattr(trainer, 'logistic_objective', observe)
    trainer.fit(records, hard_example_weight=2., objective='legacy_binary')
    assert seen


@pytest.mark.parametrize('mutation', ['duplicate', 'signature', 'infinite', 'labels', 'subgenre'])
def test_invalid_frozen_dataset_is_rejected(mutation):
    records = examples()
    if mutation == 'duplicate':
        records.append(records[0])
    elif mutation == 'signature':
        records[0]['source_signature'] = 'b' * 64
    elif mutation == 'infinite':
        records[0]['values'][0] = float('inf')
    elif mutation == 'labels':
        records[0]['labels'] = ['unknown']
    else:
        records[0]['subgenre'] = 'Unknown invented genre'
    with pytest.raises(ValueError):
        robust.train(records, inner_folds=2)


def test_cli_preserves_input_and_exports_replayable_dataset(tmp_path, monkeypatch):
    source, output, report_path = (tmp_path / name for name in ('source.json', 'model.json', 'report.json'))
    original = json.dumps({'examples': examples(), 'skipped': {'earlier_answer': 1}})
    source.write_text(original)
    monkeypatch.setattr(robust, 'CANDIDATES', {'ridge': {'regularization': 1.}})
    robust.main(['--dataset', str(source), '--output', str(output), '--report', str(report_path),
                 '--inner-folds', '2'])
    assert source.read_text() == original
    learned = model.validate(json.loads(output.read_text()))
    report = json.loads(report_path.read_text())
    snapshot = json.loads(report_path.with_suffix('.dataset.json').read_text())
    assert snapshot == json.loads(original)
    assert report['dataset_sha256'] == learned['training']['dataset_sha256']
    assert report['skipped'] == {'earlier_answer': 1}
    with pytest.raises(SystemExit):
        robust.main(['--dataset', str(source), '--output', str(source), '--report', str(report_path)])
    assert source.read_text() == original
