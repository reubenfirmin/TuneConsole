"""Bounded, independently selected research heads for the second #530 round."""
import numpy as np
from scipy.special import expit
from scipy.spatial.distance import cdist

from .external_binary_calibration import balanced_log_loss, fit as binary_fit
from .external_ranking import average_precision
from .external_training import fit_path, standardize

SEED = 53029
ALPHAS = (.1, 1., 10., 100., 1000.)


def grid(binary=False):
    result = [{'family': 'ridge', 'alpha': a} for a in ALPHAS]
    result += [{'family': 'rff', 'gamma': g, 'alpha': a, 'features': 64, 'seed': SEED}
               for g in (.5, 2.) for a in (1., 10., 100.)]
    result += [{'family': 'neighbors', 'neighbors': k, 'weighting': w}
               for k in (3, 7, 15, 31) for w in ('uniform', 'inverse_distance')]
    if binary:
        result += [{'family': 'logistic', 'alpha': a} for a in ALPHAS]
    return result


def serial(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(v) for v in value]
    return value


def scaled(model, x):
    return (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']


def linear(model, x):
    return scaled(model, x) @ np.asarray(model['weights']) + model['intercept']


def fit_models(x, y, specs):
    """Share decompositions across parameters, never across target selection."""
    result = [None] * len(specs)
    ridge_ids = [i for i, s in enumerate(specs) if s['family'] == 'ridge']
    if ridge_ids:
        for i, model in zip(ridge_ids, fit_path(x, y, [specs[i]['alpha'] for i in ridge_ids])):
            result[i] = {'kind': 'ridge', **model}
    z, medians, mean, scale = standardize(x)
    common = {'medians': medians, 'mean': mean, 'scale': scale}
    for gamma in sorted({s['gamma'] for s in specs if s['family'] == 'rff'}):
        ids = [i for i, s in enumerate(specs) if s['family'] == 'rff' and s['gamma'] == gamma]
        spec = specs[ids[0]]
        rng = np.random.default_rng(spec['seed'])
        frequencies = rng.normal(size=(x.shape[1], spec['features'])) * np.sqrt(2 * gamma / x.shape[1])
        offsets = rng.uniform(0, 2 * np.pi, spec['features'])
        phi = np.sqrt(2 / spec['features']) * np.cos(z @ frequencies + offsets)
        for i, model in zip(ids, fit_path(phi, y, [specs[i]['alpha'] for i in ids])):
            result[i] = {'kind': 'rff', **common, 'frequencies': frequencies,
                         'offsets': offsets, 'features': spec['features'], 'linear': model}
    for i, spec in enumerate(specs):
        if spec['family'] == 'neighbors':
            result[i] = {'kind': 'neighbors', **common, 'training_x': z, 'training_y': y,
                         'neighbors': spec['neighbors'], 'weighting': spec['weighting']}
        elif spec['family'] == 'logistic':
            # Separate optimizations avoid an optimizer stopping criterion shared
            # across vibe columns. Changing one target cannot alter another fit.
            columns = [binary_fit(x, y[:, j:j + 1], spec['alpha']) for j in range(y.shape[1])]
            result[i] = {'kind': 'logistic', **common,
                         'weights': np.column_stack([m['weights'][:, 0] for m in columns]),
                         'intercept': np.array([m['intercept'][0] for m in columns])}
    if any(model is None for model in result):
        raise ValueError('Unknown model family')
    return result


def predict(model, x):
    kind = model['kind']
    if kind == 'rff':
        phi = np.sqrt(2 / model['features']) * np.cos(
            scaled(model, x) @ np.asarray(model['frequencies']) + model['offsets'])
        values = linear(model['linear'], phi)
    elif kind == 'neighbors':
        z, train = scaled(model, x), np.asarray(model['training_x'])
        distance = cdist(z, train, metric='sqeuclidean')
        ids = np.argsort(distance, axis=1, kind='stable')[:, :model['neighbors']]
        d = np.take_along_axis(distance, ids, axis=1)
        weights = np.ones_like(d)
        if model['weighting'] == 'inverse_distance':
            weights = 1 / np.maximum(np.sqrt(d), 1e-8)
            exact = d <= 1e-16
            weights = np.where(exact.any(1, keepdims=True), exact.astype(float), weights)
        values = (np.asarray(model['training_y'])[ids] * weights[:, :, None]).sum(1) / weights.sum(1, keepdims=True)
    else:
        values = linear(model, x)
        if kind == 'logistic':
            return expit(values)
    return np.clip(values, 0, 1)


def column_model(model, column):
    result = dict(model)
    if model['kind'] == 'rff':
        result['linear'] = column_model({'kind': 'ridge', **model['linear']}, column)
    elif model['kind'] == 'neighbors':
        result['training_y'] = np.asarray(model['training_y'])[:, column:column + 1]
    else:
        result['weights'] = np.asarray(model['weights'])[:, column:column + 1]
        result['intercept'] = np.asarray(model['intercept'])[column:column + 1]
    return result


def replay(model, x):
    """Independent exported-form inference; deliberately does not call predict."""
    clean = np.where(np.isfinite(x), x, np.asarray(model['medians']))
    z = (clean - np.asarray(model['mean'])) / np.asarray(model['scale'])
    if model['kind'] == 'rff':
        features = np.cos(z @ np.array(model['frequencies']) + np.array(model['offsets']))
        features *= np.sqrt(2 / model['features'])
        head = model['linear']
        value = ((features - np.array(head['mean'])) / np.array(head['scale'])) @ np.array(head['weights'])
        return np.clip(value + head['intercept'], 0, 1)
    if model['kind'] == 'neighbors':
        outputs = []
        for row in z:
            squared = ((np.array(model['training_x']) - row) ** 2).sum(1)
            ids = np.argsort(squared, kind='stable')[:model['neighbors']]
            weights = np.ones(len(ids))
            if model['weighting'] == 'inverse_distance':
                weights = 1 / np.maximum(np.sqrt(squared[ids]), 1e-8)
                if (squared[ids] <= 1e-16).any():
                    weights = (squared[ids] <= 1e-16).astype(float)
            outputs.append(np.average(np.array(model['training_y'])[ids], axis=0, weights=weights))
        return np.array(outputs)
    logits = z @ np.array(model['weights']) + np.array(model['intercept'])
    if model['kind'] == 'logistic':
        return 1 / (1 + np.exp(-logits))
    return np.clip(logits, 0, 1)


def select_native(y, oof, specs):
    losses = np.abs(oof - y[:, None, :]).mean(0)
    ridge = [i for i, spec in enumerate(specs) if spec['family'] == 'ridge']
    shared = min(ridge, key=lambda i: (float(losses[i].mean()), i))
    selected = losses.argmin(0).tolist()
    return {'shared': [shared] * y.shape[1], 'independent': selected,
            'inner_mae': losses.tolist()}


def select_vibes(y, oof, split):
    """Column-local selectors. Candidate zero is the incumbent, including ties."""
    ap = np.array([[average_precision(y[:, j] > 0, oof[:, i, j])
                    if (y[:, j] > 0).any() else np.nan for j in range(y.shape[1])]
                   for i in range(oof.shape[1])])
    loss = np.array([[balanced_log_loss(y[:, j:j + 1], oof[:, i, j:j + 1])
                      for j in range(y.shape[1])] for i in range(oof.shape[1])])
    independent, guarded = [], []
    for j in range(y.shape[1]):
        if not np.isfinite(ap[:, j]).any() or len(np.unique(y[:, j] > 0)) < 2:
            independent.append(0)
            guarded.append(0)
            continue
        winner = max(range(len(ap)), key=lambda i: (round(float(ap[i, j]), 12), -i))
        independent.append(winner)
        wins = 0
        for fold in sorted(set(split)):
            mask = split == fold
            if len(np.unique(y[mask, j] > 0)) < 2:
                continue
            base = average_precision(y[mask, j] > 0, oof[mask, 0, j])
            candidate = average_precision(y[mask, j] > 0, oof[mask, winner, j])
            wins += candidate > base + 1e-12
        guarded.append(winner if ap[winner, j] - ap[0, j] >= .02 and wins >= 2 else 0)
    usable = np.isfinite(ap[0])
    shared = (max(range(len(ap)), key=lambda i: (round(float(ap[i, usable].mean()), 12), -i))
              if usable.any() else 0)
    return {'independent_ap': independent, 'guarded_ap': guarded,
            'independent_loss': loss.argmin(0).tolist(), 'shared_ap': [shared] * y.shape[1],
            'incumbent': [0] * y.shape[1], 'inner_ap': serial(np.where(np.isfinite(ap), ap, -1)),
            'inner_balanced_loss': loss.tolist()}
