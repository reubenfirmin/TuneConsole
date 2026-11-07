"""Efficient independent replay of exported research attribute-to-vibe heads."""
import numpy as np

from .mapped_vibe_rules import apply


def replay(model, x):
    """Independent numerical implementation; materialize neighbor arrays once."""
    x = np.asarray(x, dtype=float)
    z = (np.where(np.isfinite(x), x, np.asarray(model['medians']))
         - np.asarray(model['mean'])) / np.asarray(model['scale'])
    kind = model['kind']
    if kind == 'rff':
        phi = np.cos(z @ np.asarray(model['frequencies']) + np.asarray(model['offsets']))
        return replay({'kind': 'ridge', **model['linear']}, phi * np.sqrt(2 / model['features']))
    if kind == 'neighbors':
        train, targets = np.asarray(model['training_x']), np.asarray(model['training_y'])
        outputs = []
        for row in z:
            squared = np.sum((train - row) ** 2, axis=1)
            ids = np.argsort(squared, kind='stable')[:model['neighbors']]
            weights = np.ones(len(ids))
            if model['weighting'] == 'inverse_distance':
                weights = 1 / np.maximum(np.sqrt(squared[ids]), 1e-8)
                if (squared[ids] <= 1e-16).any():
                    weights = (squared[ids] <= 1e-16).astype(float)
            outputs.append(np.average(targets[ids], weights=weights, axis=0))
        return np.asarray(outputs)
    if kind == 'constant':
        return np.broadcast_to(model['value'], (len(x), len(model['value']))).copy()
    if kind != 'ridge':
        raise ValueError('Unsupported exported model: ' + kind)
    return np.clip(z @ np.asarray(model['weights']) + np.asarray(model['intercept']), 0, 1)


def feature_values(data, family):
    if family == 'attributes_embedding':
        return np.column_stack([data['attributes'], data['embedding']])
    return data[family]


def score(expert, data):
    native = replay(expert['model'], feature_values(data, expert['family']))
    return apply(expert['rule']['expression'], native, expert['target_names'])
