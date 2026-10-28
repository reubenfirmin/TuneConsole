"""Independent vibe suitability scores for ranking songs within a chosen vibe."""
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

from .external_ranking import auc, average_precision, top_mean
from .external_training import SEED, folds, standardize
from .external_transfer import VIBES, score

ALPHAS = (.1, 1., 10., 100., 1000.)


def sample_weights(y):
    positive = y.sum(0)
    negative = len(y) - positive
    mixed = (positive > 0) & (negative > 0)
    weights = np.ones_like(y, dtype=float)
    for column in np.flatnonzero(mixed):
        weights[:, column] = np.where(y[:, column] > 0, len(y) / (2 * positive[column]), len(y) / (2 * negative[column]))
    return weights


def objective(parameters, z, y, alpha):
    n, d = z.shape
    k = y.shape[1]
    weights, intercept = parameters[:d * k].reshape(d, k), parameters[d * k:]
    logits = z @ weights + intercept
    importance = sample_weights(y)
    error = importance * (expit(logits) - y) / (n * k)
    value = (importance * (np.logaddexp(0, logits) - y * logits)).mean()
    value += alpha * (weights * weights).sum() / (2 * n * k)
    # A half positive and half negative intercept observation keeps fits finite
    # when a rare vibe has no positives inside an inner training partition.
    value += .5 * (np.logaddexp(0, intercept) + np.logaddexp(0, -intercept)).sum() / (n * k)
    gradient = np.concatenate([(z.T @ error + alpha * weights / (n * k)).ravel(),
                               error.sum(0) + .5 * (2 * expit(intercept) - 1) / (n * k)])
    return float(value), gradient


def fit(x, y, alpha, *, standardize_features=True):
    z, medians, mean, scale = standardize(x)
    if not standardize_features:
        # Preserve scaling learned on a disjoint unlabeled PCA/whitening pool.
        z = z * scale
        scale = np.ones(x.shape[1])
    y = (y > 0).astype(float)
    n, d = z.shape
    k = y.shape[1]
    initial = np.zeros((d + 1) * k)
    result = minimize(objective, initial, args=(z, y, alpha), jac=True, method='L-BFGS-B',
                      options={'maxiter': 1000, 'ftol': 1e-11, 'gtol': 1e-7})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('Binary vibe calibration failed: ' + result.message)
    return {'kind': 'independent_binary', 'medians': medians, 'mean': mean, 'scale': scale,
            'weights': result.x[:d * k].reshape(d, k), 'intercept': result.x[d * k:], 'alpha': alpha}


def predict(model, x):
    z = (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']
    return expit(z @ model['weights'] + model['intercept'])


def balanced_log_loss(y, values):
    y = (y > 0).astype(float)
    p = np.clip(values, 1e-10, 1 - 1e-10)
    return float(-(sample_weights(y) * (y * np.log(p) + (1 - y) * np.log1p(-p))).mean())


def metrics(y, values):
    labels = y > 0
    per_vibe = {}
    for i, name in enumerate(VIBES):
        per_vibe[name] = {'positive_songs': int(labels[:, i].sum()),
                          'prevalence': float(labels[:, i].mean()),
                          'average_precision': average_precision(labels[:, i], values[:, i]),
                          'roc_auc': auc(labels[:, i], values[:, i]),
                          'top_five_selected_fraction': top_mean(labels[:, i].astype(float), values[:, i], 5)}
    ap = [value['average_precision'] for value in per_vibe.values() if value['average_precision'] is not None]
    roc = [value['roc_auc'] for value in per_vibe.values() if value['roc_auc'] is not None]
    return {'macro_average_precision': float(np.mean(ap)), 'macro_roc_auc': float(np.mean(roc)),
            'balanced_binary_log_loss': balanced_log_loss(y, values),
            'top_choice': score(y, values), 'per_vibe': per_vibe}


def select(x, y, groups):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = np.zeros(len(ALPHAS))
    for fold in range(3):
        train, valid = split != fold, split == fold
        for i, alpha in enumerate(ALPHAS):
            losses[i] += sum(valid) * balanced_log_loss(y[valid], predict(fit(x[train], y[train], alpha), x[valid]))
    return ALPHAS[int(losses.argmin())]
