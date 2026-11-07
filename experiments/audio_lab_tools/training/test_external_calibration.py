"""Exercise numerical continuation on actual correlated training features."""
import numpy as np

from experiments.audio_lab_tools.training.external_calibration import fit, probabilities


def test_softmax_continues_iteration_budget_without_changing_the_objective():
    rng = np.random.default_rng(530)
    latent = rng.normal(size=(180, 4))
    x = latent @ rng.normal(size=(4, 30)) + rng.normal(scale=.001, size=(180, 30))
    y = np.eye(3)[(latent @ rng.normal(size=(4, 3))).argmax(1)]
    ordinary = fit(x, y, .1)
    continued = fit(x, y, .1, initial_iterations=1)
    assert continued['optimization']['converged']
    assert continued['optimization']['iterations'] > 1
    assert abs(continued['optimization']['objective'] - ordinary['optimization']['objective']) < 1e-7
    predictions = probabilities(continued, x)
    assert np.isfinite(predictions).all()
    assert np.allclose(predictions.sum(1), 1)
    assert (predictions.argmax(1) == y.argmax(1)).mean() > .95
