"""Recording-balanced ridge selection with one fixed validation window per song."""
import numpy as np

from ..training.external_training import folds
from .individual_protocol import group_guard
from ..audio.quality_audio import recording_weights

ALPHAS = (.1, 1., 10., 100., 1000.)


def fit(x, y, ids, alpha):
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Unknown native values cannot enter ridge fitting')
    w = recording_weights(ids)
    mean, ym = np.average(x, axis=0, weights=w), np.average(y, axis=0, weights=w)
    scale = np.sqrt(np.average((x - mean) ** 2, axis=0, weights=w))
    scale = np.where(scale < 1e-8, 1., scale)
    z = (x - mean) / scale
    weights = np.linalg.solve(z.T @ (z * w[:, None]) + alpha * np.eye(z.shape[1]),
                              z.T @ ((y - ym) * w[:, None]))
    return {'kind': 'ridge', 'medians': mean.tolist(), 'mean': mean.tolist(), 'scale': scale.tolist(),
            'weights': weights.tolist(), 'intercept': ym.tolist(), 'alpha': alpha}


def predict(model, x):
    z = (x - model['mean']) / model['scale']
    return np.clip(z @ np.asarray(model['weights']) + model['intercept'], 0, 1)


def select(data, mask, all_windows):
    ids = np.flatnonzero(mask)
    assignment = folds(data['groups'][mask], count=3, seed=70701)
    group_guard(data['groups'][mask], assignment)
    predictions = np.full((len(ids), len(ALPHAS), 2), np.nan)
    trace = {'training_ids': data['ids'][mask].tolist(), 'training_groups': data['groups'][mask].tolist(),
             'folds': assignment.tolist(), 'all_windows': all_windows, 'selection': []}
    for f in range(3):
        train = ids[assignment != f]
        valid = ids[(assignment == f) & data['selected'][ids]]
        if not all_windows:
            train = train[data['selected'][train]]
        for i, alpha in enumerate(ALPHAS):
            model = fit(data['x'][train], data['y'][train], data['recordings'][train], alpha)
            positions = np.flatnonzero((assignment == f) & data['selected'][ids])
            predictions[positions, i] = predict(model, data['x'][valid])
        trace['selection'].append({'training_groups': data['groups'][train].tolist(),
                                   'validation_groups': data['groups'][valid].tolist()})
    keep = data['selected'][ids]
    if not np.isfinite(predictions[keep]).all():
        raise ValueError('Incomplete matched validation rows')
    loss = ((predictions[keep] - data['y'][ids[keep], None]) ** 2).mean(0)
    chosen = loss.argmin(0)
    train = ids if all_windows else ids[data['selected'][ids]]
    models = {int(i): fit(data['x'][train], data['y'][train], data['recordings'][train], ALPHAS[i]) for i in set(chosen)}
    model = {**models[int(chosen[0])],
        'weights': np.column_stack([np.asarray(models[int(i)]['weights'])[:, col] for col, i in enumerate(chosen)]).tolist(),
        'selected_alpha': [ALPHAS[i] for i in chosen]}
    trace.update(selected_alpha=model['selected_alpha'], inner_mse=loss.tolist(), fit_recordings=len(set(data['recordings'][train])),
                 fit_windows=len(train), fit_groups=data['groups'][train].tolist())
    return model, trace
