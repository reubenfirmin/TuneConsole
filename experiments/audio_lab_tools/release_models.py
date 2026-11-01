"""Small nested Release mappings and masked, source-balanced native tasks."""
import numpy as np
from scipy.linalg import solve

from . import individual_protocol as protocol
from .external_training import folds
from .focus_experiment import head_predictions
from .vibe_specialist_models import serial

SEED = 61430
WEIGHTS = (0., .25, .5, .75, 1.)
PENALTIES = (.01, .1, 1., 10.)


def split(data, count=3):
    result = folds(data['groups'], count=count, seed=SEED)
    protocol.group_guard(data['groups'], result)
    return result


def subset(data, keep):
    return {key: value[keep] for key, value in data.items()}


def native_fit(data):
    heads, trace = protocol.select(data['x'], data['y'][:, None], split(data))
    return heads[0], trace


def native_predict(head, x, independent=False):
    return head_predictions([head], x, independent)[:, 0]


def crossfit(data):
    outer = split(data)
    predicted = np.empty(len(data['y']))
    trace = []
    for fold in sorted(set(outer)):
        train, valid = outer != fold, outer == fold
        head, selection = native_fit(subset(data, train))
        predicted[valid] = native_predict(head, data['x'][valid])
        trace.append({'train_ids': data['ids'][train].tolist(),
                      'valid_ids': data['ids'][valid].tolist(), 'selection': selection})
    return predicted, trace


def affine_fit(score, target):
    centered = score - score.mean()
    variance = np.dot(centered, centered)
    slope = float(np.clip(np.dot(centered, target - target.mean()) / variance, 0, 4)) if variance > 1e-15 else 0.
    return {'slope': slope, 'intercept': float(target.mean() - slope * score.mean())}


def affine_predict(calibration, score):
    return np.clip(calibration['slope'] * score + calibration['intercept'], 0, 1)


def mapping_fit(anchor, auxiliary):
    """Outer callers exclude test identities before this fully nested selector."""
    auxiliary_head, auxiliary_trace = native_fit(auxiliary)
    auxiliary_scores = native_predict(auxiliary_head, anchor['x'])
    outer = split(anchor)
    predictions = np.empty((len(anchor['y']), len(WEIGHTS)))
    traces = []
    for fold in sorted(set(outer)):
        train, valid = outer != fold, outer == fold
        training = subset(anchor, train)
        anchor_oof, trace = crossfit(training)
        anchor_head, selection = native_fit(training)
        validation_scores = native_predict(anchor_head, anchor['x'][valid])
        for index, weight in enumerate(WEIGHTS):
            training_score = (1 - weight) * anchor_oof + weight * auxiliary_scores[train]
            calibration = affine_fit(training_score, anchor['y'][train])
            score = (1 - weight) * validation_scores + weight * auxiliary_scores[valid]
            predictions[valid, index] = affine_predict(calibration, score)
        traces.append({'selection_train_ids': anchor['ids'][train].tolist(),
                       'selection_valid_ids': anchor['ids'][valid].tolist(),
                       'crossfit': trace, 'base_selection': selection})
    losses = np.mean((predictions - anchor['y'][:, None]) ** 2, axis=0)
    chosen = int(np.argmin(losses))
    anchor_oof, final_trace = crossfit(anchor)
    anchor_head, anchor_selection = native_fit(anchor)
    weight = WEIGHTS[chosen]
    calibration = affine_fit((1 - weight) * anchor_oof + weight * auxiliary_scores, anchor['y'])
    model = {'kind': 'release_monotone', 'anchor': anchor_head, 'auxiliary': auxiliary_head,
             'weight': weight, 'calibration': calibration,
             'zero_weight_calibration': affine_fit(anchor_oof, anchor['y'])}
    return model, {'weights': WEIGHTS, 'inner_mse': losses.tolist(), 'selected_weight': weight,
                   'mapping_selection': traces, 'final_crossfit': final_trace,
                   'anchor_selection': anchor_selection, 'auxiliary_selection': auxiliary_trace,
                   'anchor_training_ids': anchor['ids'].tolist(),
                   'auxiliary_training_ids': auxiliary['ids'].tolist()}


def mapping_predict(model, x, independent=False, zero=False):
    a = native_predict(model['anchor'], x, independent)
    if zero:
        return affine_predict(model['zero_weight_calibration'], a)
    b = native_predict(model['auxiliary'], x, independent)
    return affine_predict(model['calibration'], (1 - model['weight']) * a + model['weight'] * b)


def tasks_fit(datasets, penalty):
    """Each observed native task has its own loss; absent labels are never zero."""
    names = sorted(datasets)
    all_x = np.concatenate([datasets[name]['x'] for name in names])
    if not np.isfinite(all_x).all():
        raise ValueError('Release embeddings must be finite')
    weights = np.concatenate([np.full(len(datasets[name]['y']), 1 / len(names) / len(datasets[name]['y']))
                              for name in names])
    mean = np.sum(all_x * weights[:, None], axis=0)
    scale = np.sqrt(np.sum((all_x - mean) ** 2 * weights[:, None], axis=0))
    scale[scale < 1e-12] = 1
    dimension = all_x.shape[1]
    parameter_count = dimension * (len(names) + 1)
    gram = np.eye(parameter_count) * penalty
    rhs = np.zeros(parameter_count)
    centers, targets = {}, {}
    for index, name in enumerate(names):
        data = datasets[name]
        z = (data['x'] - mean) / scale
        centers[name], targets[name] = z.mean(axis=0), float(data['y'].mean())
        z = z - centers[name]
        design = np.zeros((len(z), parameter_count))
        design[:, :dimension] = z
        design[:, (index + 1) * dimension:(index + 2) * dimension] = z
        weight = 1 / len(names) / len(z)
        gram += weight * design.T @ design
        rhs += weight * design.T @ (data['y'] - targets[name])
    coefficients = solve(gram, rhs, assume_a='pos')
    effective = np.column_stack([coefficients[:dimension] + coefficients[(i + 1) * dimension:(i + 2) * dimension]
                                 for i in range(len(names))])
    intercept = np.asarray([targets[name] - centers[name] @ effective[:, i] for i, name in enumerate(names)])
    return serial({'kind': 'release_native_tasks', 'targets': names, 'mean': mean, 'scale': scale,
                   'weights': effective, 'intercept': intercept, 'penalty': penalty,
                   'shared_weights': coefficients[:dimension],
                   'deviation_weights': coefficients[dimension:].reshape(len(names), dimension),
                   'source_rows': {name: len(datasets[name]['y']) for name in names}})


def tasks_predict(model, x, target):
    column = model['targets'].index(target)
    z = (x - np.asarray(model['mean'])) / np.asarray(model['scale'])
    return np.clip(z @ np.asarray(model['weights'])[:, column] + model['intercept'][column], 0, 1)


def tasks_select(datasets):
    assignments = {name: split(data) for name, data in datasets.items()}
    losses = []
    trace = []
    for penalty in PENALTIES:
        predicted = {name: np.empty(len(data['y'])) for name, data in datasets.items()}
        for fold in range(3):
            training = {name: subset(data, assignments[name] != fold) for name, data in datasets.items()}
            model = tasks_fit(training, penalty)
            for name, data in datasets.items():
                valid = assignments[name] == fold
                predicted[name][valid] = tasks_predict(model, data['x'][valid], name)
        per_source = {name: float(np.mean((predicted[name] - data['y']) ** 2)) for name, data in datasets.items()}
        losses.append(float(np.mean(list(per_source.values()))))
        trace.append(per_source)
    chosen = int(np.argmin(losses))
    return tasks_fit(datasets, PENALTIES[chosen]), {
        'penalties': PENALTIES, 'macro_native_mse': losses, 'per_source_mse': trace,
        'selected_penalty': PENALTIES[chosen],
        'training_ids': {name: data['ids'].tolist() for name, data in datasets.items()},
        'inner_folds': {name: value.tolist() for name, value in assignments.items()}}


def independent_tasks_predict(model, x, target):
    """Replay the shared/deviation representation instead of effective weights."""
    index = model['targets'].index(target)
    coefficients = np.array(model['shared_weights']) + np.array(model['deviation_weights'][index])
    output = []
    for row in x:
        value = model['intercept'][index]
        for j, coefficient in enumerate(coefficients):
            value += (row[j] - model['mean'][j]) / model['scale'][j] * coefficient
        output.append(min(1., max(0., float(value))))
    return np.array(output)
