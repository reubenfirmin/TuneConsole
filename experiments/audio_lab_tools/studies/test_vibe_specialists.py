"""Leakage, independent selection, export and evidence integrity checks."""
import json

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.studies import vibe_specialist_models as model
from experiments.audio_lab_tools.studies import vibe_specialist_round as run
from experiments.audio_lab_tools.support.external_experiment import digest, save
from experiments.audio_lab_tools.training.external_training import folds


@pytest.mark.parametrize('family', ['ridge', 'rff', 'neighbors', 'logistic'])
def test_serialized_column_replays_real_model_and_uses_training_scaling(family):
    rng = np.random.default_rng(67)
    x = rng.normal(size=(21, 7))
    x[0, 0] = np.nan
    y = (rng.normal(size=(21, 3)) > 0).astype(float)
    spec = next(s for s in model.grid(binary=True) if s['family'] == family)
    with threadpool_limits(limits=1):
        fitted = model.fit_models(x, y, [spec])[0]
    test = rng.normal(size=(5, 7)) + 100  # A validation-only distribution change.
    before = model.serial(fitted)
    expected = model.predict(fitted, test)[:, 1:2]
    exported = json.loads(json.dumps(model.serial(model.column_model(fitted, 1))))
    np.testing.assert_allclose(model.replay(exported, test), expected, atol=1e-12)
    assert model.serial(fitted) == before
    assert fitted['mean'][1] == pytest.approx(np.mean(x[:, 1]))


def test_other_vibe_label_changes_do_not_change_independent_choices_or_fits():
    rng = np.random.default_rng(71)
    y = (rng.uniform(size=(30, 3)) > .65).astype(float)
    scores = rng.uniform(size=(30, 7, 3))
    split = np.arange(30) % 3
    first = model.select_vibes(y, scores, split)
    changed = y.copy()
    changed[:, 0] = 1 - changed[:, 0]
    second = model.select_vibes(changed, scores, split)
    for name in ('independent_ap', 'guarded_ap', 'independent_loss'):
        assert first[name][1:] == second[name][1:]
    x = rng.normal(size=(30, 6))
    specs = [{'family': 'logistic', 'alpha': 1.}]
    with threadpool_limits(limits=1):
        a = model.fit_models(x, y, specs)[0]
        b = model.fit_models(x, changed, specs)[0]
    np.testing.assert_array_equal(model.predict(a, x)[:, 1:], model.predict(b, x)[:, 1:])


def test_guard_requires_predeclared_gain_and_multiple_inner_fold_wins():
    # Winner is perfect on one fold and unchanged on two: insufficient support.
    y = np.tile([0., 1., 0., 1.], 3)[:, None]
    scores = np.zeros((12, 2, 1)) + .5
    scores[:4, 1, 0] = y[:4, 0]
    split = np.repeat(np.arange(3), 4)
    chosen = model.select_vibes(y, scores, split)
    assert chosen['independent_ap'] == [1]
    assert chosen['guarded_ap'] == [0]
    scores[4:8, 1, 0] = y[4:8, 0]
    assert model.select_vibes(y, scores, split)['guarded_ap'] == [1]
    # Ties and all-zero or all-one vibe columns keep the incumbent for AP.
    for labels in (np.zeros_like(y), np.ones_like(y), y):
        choice = model.select_vibes(labels, np.ones_like(scores), split)
        assert choice['independent_ap'] == choice['guarded_ap'] == [0]


def test_held_out_target_perturbation_cannot_enter_native_selection():
    rng = np.random.default_rng(72)
    x, y = rng.normal(size=(36, 5)), rng.uniform(size=(36, 2))
    groups = np.array([f'artist:{i // 2}' for i in range(36)])
    outer = folds(groups, seed=model.SEED)
    train, valid = outer != 0, outer == 0
    assert set(groups[train]).isdisjoint(set(groups[valid]))
    specs = [s for s in model.grid() if s['family'] == 'ridge']
    with threadpool_limits(limits=1):
        first, _ = run.candidate_oof(x[train], y[train], groups[train], specs)
        changed = y.copy()
        changed[valid] = 100
        second, _ = run.candidate_oof(x[train], changed[train], groups[train], specs)
    np.testing.assert_array_equal(first, second)
    assert model.select_native(y[train], first, specs) == model.select_native(changed[train], second, specs)


def test_receipts_reject_mutated_or_escaping_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(run, 'EXPERIMENTS', tmp_path)
    directory = tmp_path / 'round'
    directory.mkdir()
    artifact = directory / 'model.json'
    save(artifact, {'model': 1})
    receipt = {'state': 'complete', 'fingerprint': 'plan', 'outputs': {'model.json': digest(artifact)}}
    save(directory / 'native.receipt.json', receipt)
    assert run.receipt_valid(directory, 'native', 'plan')
    assert not run.receipt_valid(directory, 'native', 'other-plan')
    save(artifact, {'model': 2})
    with pytest.raises(ValueError, match='output changed'):
        run.receipt_valid(directory, 'native', 'plan')
    receipt['outputs'] = {'../elsewhere.json': 'bad'}
    save(directory / 'native.receipt.json', receipt)
    with pytest.raises(ValueError, match='output changed'):
        run.receipt_valid(directory, 'native', 'plan')
    with pytest.raises(ValueError, match='inside experiments'):
        run.inside(tmp_path.parent / 'escape.json')


def test_frozen_inputs_and_code_are_required_for_resumption(tmp_path, monkeypatch):
    monkeypatch.setattr(run, 'EXPERIMENTS', tmp_path)
    monkeypatch.setattr(run, 'sources', lambda: {'trainer.py': 'code-hash'})
    monkeypatch.setattr(run, 'versions', lambda: {'runtime': 'version'})
    frozen = tmp_path / 'inputs.npz'
    run.npz(frozen, y=np.array([[1., 0.]]))
    save(tmp_path / 'plan.json', {'code': {'trainer.py': 'code-hash'},
         'versions': {'runtime': 'version'}, 'inputs': {'inputs.npz': digest(frozen)}})
    # Code archive validation must also pass before returning the plan.
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/trainer.py').write_text('frozen')
    expected = digest(tmp_path / 'source/trainer.py')
    monkeypatch.setattr(run, 'sources', lambda: {'trainer.py': expected})
    plan = run.load(tmp_path / 'plan.json')
    plan['code'] = {'trainer.py': expected}
    save(tmp_path / 'plan.json', plan)
    run.validate(tmp_path)
    run.npz(frozen, y=np.array([[0., 1.]]))
    with pytest.raises(ValueError, match='Frozen input changed'):
        run.validate(tmp_path)
    monkeypatch.setattr(run, 'versions', lambda: {'runtime': 'new-version'})
    with pytest.raises(ValueError, match='runtime changed'):
        run.validate(tmp_path)
