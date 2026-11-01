"""Reuse requires identical scientific inputs, not merely an existing model."""
import numpy as np
import pytest

from experiments.audio_lab_tools import vibe_specialist_all as extension
from experiments.audio_lab_tools import vibe_specialist_round as engine
from experiments.audio_lab_tools.external_experiment import digest, save


def test_native_reuse_checks_actual_arrays_grid_and_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'EXPERIMENTS', tmp_path)
    prior, new = tmp_path / 'prior', tmp_path / 'new'
    for path in (prior, new):
        path.mkdir()
        save(path / 'plan.json', {'native_grid': [{'family': 'ridge', 'alpha': 1}],
            'seed': 1, 'versions': {'numpy': 'same'},
            'code': {'run.py': str(path), 'trainer.py': 'unchanged'}})
        save(path / 'inputs/example.json', {'groups': ['artist:a', 'artist:b'], 'targets': ['calm']})
        engine.npz(path / 'inputs/example.npz', x=np.array([[1.], [2.]]), y=np.array([[0.], [1.]]))
    artifact = prior / 'model.json'
    save(artifact, {'weights': [1.]})
    save(prior / 'native-example.receipt.json', {'state': 'complete',
        'fingerprint': digest(prior / 'plan.json'), 'outputs': {'model.json': digest(artifact)}})
    assert extension.verify_reuse(prior, new, 'example')['state'] == 'complete'
    engine.npz(new / 'inputs/example.npz', x=np.array([[1.], [3.]]), y=np.array([[0.], [1.]]))
    with pytest.raises(ValueError, match='arrays changed'):
        extension.verify_reuse(prior, new, 'example')
    engine.npz(new / 'inputs/example.npz', x=np.array([[1.], [2.]]), y=np.array([[0.], [1.]]))
    artifact.write_text('{}')
    with pytest.raises(ValueError, match='output changed'):
        extension.verify_reuse(prior, new, 'example')
