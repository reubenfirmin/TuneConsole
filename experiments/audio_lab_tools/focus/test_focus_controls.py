"""Independent arithmetic and leakage challenges for the Focus controls."""
import json
import sys

import numpy as np
import pytest

from experiments.audio_lab_tools.focus import focus_controls as models, focus_listeners as listeners
from experiments.audio_lab_tools.focus import focus_controls_experiment as experiment
from experiments.audio_lab_tools.relax import relax_experiment
from experiments.audio_lab_tools.focus.focus_controls_experiment import verify_trace


def test_residual_removes_raw_slopes_and_retains_orthogonal_signal():
    head = {'kind': 'ridge', 'mean': [1., 2., 3.], 'medians': [0.] * 3,
            'scale': [2., 4., 1.], 'weights': [[2.], [0.], [0.]], 'intercept': [.2]}
    native = {'heads': [head], 'mean': [1., 2., 3.], 'basis': [[1.], [0.], [0.]]}
    x = np.array([[5., 7., 9.], [-4., 3., 8.]])
    r = models.representation(native, x)
    np.testing.assert_allclose(r['residual'], [[0., 5., 6.], [0., 1., 5.]])
    np.testing.assert_allclose(r['emotion_raw'], [[4.2], [-4.8]])
    np.testing.assert_allclose(r['emotion'], [[1.], [0.]])
    np.testing.assert_allclose(r['residual'] @ models.slopes([head], 3), 0)


def test_zero_native_rank_preserves_centered_embedding():
    native = {'heads': [{'kind': 'constant', 'value': [.2]}], 'mean': [1., 2.], 'basis': [[], []]}
    r = models.representation(native, np.array([[5., 7.]]))
    np.testing.assert_array_equal(r['residual'], [[4., 5.]])


def test_task_offsets_are_added_before_clipping():
    head = {'kind': 'ridge', 'mean': [0.], 'medians': [0.], 'scale': [1.], 'weights': [[1.]], 'intercept': [0.]}
    result = models.offset_predictions(head, np.array([[-.1], [1.1]]), [-.2, .2])
    np.testing.assert_allclose(result, [[0, .1], [.9, 1]])


def test_pca_training_transform_does_not_observe_heldout_points():
    x = np.random.default_rng(6).normal(size=(25, 12))
    transform = models.pca(x)
    mean = np.asarray(transform['mean']).copy()
    result = models.project(transform, np.full((2, 12), 1e6))
    assert np.isfinite(result).all()
    np.testing.assert_array_equal(transform['mean'], mean)
    np.testing.assert_allclose(models.project(transform, x).mean(0), 0, atol=1e-14)


def test_native_exclusion_audit_rejects_deep_inner_leakage():
    clean = {'training_groups': [3, 4, 5], 'inner_folds': [0, 1, 2]}
    trace = {'training_groups': [1, 2, 3, 4, 5], 'selection': {}, 'final_native': clean,
             'final_crossfit': {'0': {'validation_groups': [3], 'selection': clean}}}
    with pytest.raises(ValueError, match='Validation component'):
        verify_trace(trace, [0])


def listener_fixture():
    rng = np.random.default_rng(8)
    user = np.repeat(np.arange(10), 10)
    track = np.tile(np.arange(10), 10)
    activities = rng.integers(0, 2, size=(100, 9)).astype(float)
    return {'user': user, 'track': track, 'artist': track, 'activities': activities}


def test_heldout_listener_has_exact_base_fallback():
    d = listener_fixture()
    prediction, support = listeners.probabilities(d, d['user'] % 5)
    assert not support.any()
    np.testing.assert_allclose(prediction[:, 1:], np.repeat(prediction[:, :1], 2, axis=1), atol=1e-15)


def test_listener_predictions_cannot_see_heldout_activity_answers():
    d = listener_fixture()
    split = d['artist'] % 5
    prediction, _ = listeners.probabilities(d, split)
    changed = {**d, 'activities': d['activities'].copy()}
    changed['activities'][split == 0] = 1 - changed['activities'][split == 0]
    alternative, _ = listeners.probabilities(changed, split)
    np.testing.assert_array_equal(prediction[split == 0], alternative[split == 0])


def test_beta_smoothing_matches_hand_calculation():
    values = np.array([1., 0., 1., 0.])
    estimate, counts, prior = listeners.smooth(values, np.array([0, 0, 1, 2]), np.array([1, 1, 1, 0], dtype=bool), 3, 2)
    assert prior == 2 / 3
    np.testing.assert_allclose(estimate, [(1 + 4 / 3) / 4, (1 + 4 / 3) / 3, 2 / 3])
    np.testing.assert_array_equal(counts, [2, 1, 0])


@pytest.fixture
def frozen_controls(tmp_path, monkeypatch):
    """A completed attempt whose archived CLI predates the current shared CLI."""
    directory = tmp_path / 'attempt'
    current = tmp_path / 'current'
    (directory / 'source').mkdir(parents=True)
    (directory / 'inputs').mkdir()
    current.mkdir()
    (directory / 'source/run.py').write_text('archived executable')
    (current / 'run.py').write_text('later unrelated CLI commands')
    (directory / 'inputs/music.bin').write_bytes(b'frozen music')
    reference = tmp_path / 'reference.bin'
    reference.write_bytes(b'referenced previous output')
    plan = {'runtime': experiment.runtime(),
            'code': {'run.py': experiment.digest(directory / 'source/run.py')},
            'inputs': {'inputs/music.bin': experiment.digest(directory / 'inputs/music.bin')},
            'references': {str(reference): experiment.digest(reference)}}
    (directory / 'plan.json').write_text(json.dumps(plan))
    for stage in ('outer-0', 'outer-1', 'outer-2', 'outer-3', 'outer-4', 'listeners', 'evaluation'):
        output = directory / (stage + '.output')
        output.write_bytes(stage.encode())
        (directory / (stage + '.receipt.json')).write_text(json.dumps({
            'plan_sha256': experiment.digest(directory / 'plan.json'),
            'outputs': {output.name: experiment.digest(output)}}))
    monkeypatch.setattr(relax_experiment, 'EXPERIMENTS', current)
    monkeypatch.setattr(experiment, 'inside', lambda path: path.resolve())
    return directory, current, reference


def controls_command(monkeypatch, directory, action):
    monkeypatch.setattr(sys, 'argv', ['focus-controls', action, '--directory', str(directory)])
    experiment.main()


def test_read_only_verification_accepts_later_cli_without_changing_attempt(frozen_controls, monkeypatch):
    directory, _, _ = frozen_controls
    before = {path.relative_to(directory): path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    controls_command(monkeypatch, directory, 'verify')
    assert {path.relative_to(directory): path.read_bytes() for path in directory.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('damage,expected', [
    ('inputs/music.bin', 'Frozen input changed'),
    ('source/run.py', 'Archived code changed'),
    ('reference', 'Referenced artifact changed'),
    *((stage + '.output', 'Receipt output changed') for stage in
      ('outer-0', 'outer-1', 'outer-2', 'outer-3', 'outer-4', 'listeners', 'evaluation')),
])
def test_historical_verification_still_rejects_corrupt_evidence(frozen_controls, monkeypatch, damage, expected):
    directory, _, reference = frozen_controls
    (reference if damage == 'reference' else directory / damage).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match=expected):
        controls_command(monkeypatch, directory, 'verify')


@pytest.mark.parametrize('action', ['fit', 'listeners', 'evaluate'])
@pytest.mark.parametrize('mismatch', ['code', 'runtime'])
def test_execution_refuses_changed_code_or_numerical_runtime(frozen_controls, monkeypatch, action, mismatch):
    directory, current, _ = frozen_controls
    expected = 'Execution code changed'
    if mismatch == 'runtime':
        (current / 'run.py').write_bytes((directory / 'source/run.py').read_bytes())
        monkeypatch.setattr(relax_experiment, 'runtime', lambda: {'python': 'changed numerical runtime'})
        expected = 'Numerical runtime changed'
    with pytest.raises(ValueError, match=expected):
        controls_command(monkeypatch, directory, action)
