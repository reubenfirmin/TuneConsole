"""Scalar bounded calibration with grouped, training-only family/threshold selection."""
import math

import numpy as np

from .external_training import folds

FAMILIES = ('identity', 'mean', 'affine', 'isotonic')
THRESHOLDS = (.5, .6, .7, .8, .9, .95)
SEED = 572029


def vectors(x, y):
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if (x.ndim != 1 or x.shape != y.shape or not len(x)
            or not np.isfinite(x).all() or not np.isfinite(y).all()
            or x.min() < 0 or x.max() > 1 or y.min() < 0 or y.max() > 1):
        raise ValueError('Expected nonempty bounded score and target vectors')
    return x, y


def fit(family, x, y):
    x, y = vectors(x, y)
    if family == 'identity':
        return {'family': family}
    if family == 'mean':
        return {'family': family, 'mean': float(y.mean())}
    if family == 'affine':
        variance = np.mean((x - x.mean()) ** 2)
        slope = float(np.clip(np.mean((x - x.mean()) * (y - y.mean())) / variance, 0, 4)) if variance > 1e-14 else 0.
        return {'family': family, 'slope': slope, 'intercept': float(y.mean() - slope * x.mean())}
    if family != 'isotonic':
        raise ValueError('Unknown calibration family')
    points, inverse, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.bincount(inverse, weights=y)
    blocks = []
    for i, (total, count) in enumerate(zip(sums, counts, strict=True)):
        blocks.append([i, i + 1, float(total), int(count)])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    fitted = np.empty(len(points))
    for start, end, total, count in blocks:
        fitted[start:end] = total / count
    return {'family': family, 'x': points.tolist(), 'y': fitted.tolist()}


def predict(model, x):
    x = np.asarray(x, dtype=float)
    if x.ndim != 1 or not np.isfinite(x).all() or (len(x) and (x.min() < 0 or x.max() > 1)):
        raise ValueError('Invalid calibration input')
    family = model['family']
    if family == 'identity':
        value = x.copy()
    elif family == 'mean':
        value = np.full(len(x), model['mean'])
    elif family == 'affine':
        value = model['intercept'] + model['slope'] * x
    elif family == 'isotonic':
        value = np.interp(x, model['x'], model['y'])
    else:
        raise ValueError('Unknown calibration family')
    return np.clip(value, 0, 1)


def check_groups(groups, split):
    groups, split = np.asarray(groups), np.asarray(split)
    if groups.ndim != 1 or groups.shape != split.shape or not len(groups):
        raise ValueError('Invalid grouped folds')
    if any(len(set(split[groups == group])) != 1 for group in set(groups)):
        raise ValueError('Identity group crosses folds')


def select(x, y, groups):
    x, y = vectors(x, y)
    split = folds(groups, count=3, seed=SEED)
    check_groups(groups, split)
    oof, losses = {}, {}
    for family in FAMILIES:
        scores = np.empty(len(x))
        for fold in range(3):
            train, test = split != fold, split == fold
            scores[test] = predict(fit(family, x[train], y[train]), x[test])
        oof[family] = scores
        losses[family] = float(np.mean([np.mean((scores[split == f] - y[split == f]) ** 2) for f in range(3)]))
    winner = min(FAMILIES, key=lambda name: (round(losses[name], 12), FAMILIES.index(name)))
    return winner, losses, oof[winner], split


def wilson_lower(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    return (p + z*z/(2*total) - z*math.sqrt(p*(1-p)/total + z*z/(4*total*total))) / (1+z*z/total)


def threshold_stats(y, scores, groups, threshold):
    y, scores = vectors(y, scores)
    groups = np.asarray(groups)
    if groups.shape != y.shape:
        raise ValueError('Invalid threshold groups')
    if threshold is None:
        selected = np.zeros(len(y), dtype=bool)
    else:
        thresholds = np.asarray(threshold)
        selected = scores >= thresholds
    selected_groups = sorted(set(groups[selected]))
    successes = sum(bool((y[selected & (groups == group)] >= .75).all()) for group in selected_groups)
    lower = wilson_lower(successes, len(selected_groups))
    return {'selected_rows': int(selected.sum()), 'selected_groups': len(selected_groups),
            'row_precision': float(np.mean(y[selected] >= .75)) if selected.any() else None,
            'all_members_high_groups': successes, 'group_precision_lower_95': lower,
            'passes': bool(selected.sum() >= 20 and len(selected_groups) >= 15 and lower is not None and lower >= .8)}


def choose_threshold(y, scores, groups):
    details = {str(t): threshold_stats(y, scores, groups, t) for t in THRESHOLDS}
    selected = next((t for t in THRESHOLDS if details[str(t)]['passes']), None)
    return selected, details


def nested(x, y, groups, outer):
    """No held-out outcome enters its own calibrator, family or threshold fit."""
    x, y = vectors(x, y)
    groups, outer = np.asarray(groups), np.asarray(outer)
    check_groups(groups, outer)
    if len(x) != len(groups) or set(outer) != set(range(5)):
        raise ValueError('Expected five grouped outer folds')
    predictions = {family: np.empty(len(x)) for family in (*FAMILIES, 'selected')}
    thresholds = np.full(len(x), np.nan)
    details, models = {}, {}
    for fold in range(5):
        train, test = outer != fold, outer == fold
        family, losses, inner_scores, inner_split = select(x[train], y[train], groups[train])
        model = fit(family, x[train], y[train])
        models[fold] = model
        predictions['selected'][test] = predict(model, x[test])
        for alternative in FAMILIES:
            predictions[alternative][test] = predict(fit(alternative, x[train], y[train]), x[test])
        threshold, diagnostics = choose_threshold(y[train], inner_scores, groups[train])
        if threshold is not None:
            thresholds[test] = threshold
        details[str(fold)] = {'family': family, 'inner_mse': losses, 'threshold': threshold,
                             'threshold_training': diagnostics, 'training_indices': np.flatnonzero(train).tolist(),
                             'evaluation_indices': np.flatnonzero(test).tolist(),
                             'inner_fold_for_training_indices': inner_split.tolist()}
    return predictions, thresholds, details, models
