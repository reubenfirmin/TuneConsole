"""Grouped probability calibration alternatives for the preserved six vibes."""
import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp

from .external_training import SEED, folds, standardize
from .external_transfer import score

ALPHAS = (.1, 1., 10., 100., 1000.)


def probabilities(model, x):
    clean = np.where(np.isfinite(x), x, model['medians'])
    z = (clean - model['mean']) / model['scale']
    logits = z @ model['weights'] + model['intercept']
    return np.exp(logits - logsumexp(logits, axis=1, keepdims=True))


def fit(x, y, alpha):
    z, medians, mean, scale = standardize(x)
    n, d = z.shape
    count = y.shape[1]
    prior = (y.sum(0) + .5) / (n + .5 * count)
    initial = np.concatenate([np.zeros(d * count), np.log(prior)])

    def objective(parameters):
        weights = parameters[:d * count].reshape(d, count)
        intercept = parameters[d * count:]
        logits = z @ weights + intercept
        logp = logits - logsumexp(logits, axis=1, keepdims=True)
        error = (np.exp(logp) - y) / n
        value = -(y * logp).sum() / n + alpha * (weights * weights).sum() / (2 * n)
        gradient = np.concatenate([(z.T @ error + alpha * weights / n).ravel(), error.sum(0)])
        return value, gradient

    result = minimize(objective, initial, jac=True, method='L-BFGS-B',
                      options={'maxiter': 500, 'ftol': 1e-11, 'gtol': 1e-7})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('Softmax fit failed: ' + result.message)
    return {'kind': 'softmax', 'medians': medians, 'mean': mean, 'scale': scale,
            'weights': result.x[:d * count].reshape(d, count),
            'intercept': result.x[d * count:], 'alpha': alpha}


def fit_selected(x, y, groups):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = np.zeros(len(ALPHAS))
    for fold in range(3):
        train, valid = split != fold, split == fold
        for i, alpha in enumerate(ALPHAS):
            losses[i] += sum(valid) * score(y[valid], probabilities(fit(x[train], y[train], alpha), x[valid]))['acceptable_log_loss']
    return fit(x, y, ALPHAS[int(np.argmin(losses))])
