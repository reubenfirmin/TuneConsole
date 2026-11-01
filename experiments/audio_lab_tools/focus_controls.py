"""Controlled representation and task models for #637/#638."""
import numpy as np

from . import individual_protocol as protocol
from .focus_experiment import head_predictions, native_predict
from .focus_mapping import CAL, EMO, candidates, protect, selected_heads, split

MAPS = ('emotion', 'residual', 'emotion_residual', 'emotion_raw')
METHODS = (*MAPS, 'direct', 'pca9', 'random9', 'mean', 'task_pooled', 'task_offset')
EMOTIONS = ['amazement', 'solemnity', 'tenderness', 'nostalgia', 'calmness',
            'power', 'joyful_activation', 'tension', 'sadness']


def raw_heads(heads, x):
    return np.column_stack([np.full(len(x), h['value'][0]) if h['kind'] == 'constant'
                            else protocol.learner.linear(h, x)[:, 0] for h in heads])


def slopes(heads, dimension):
    return np.column_stack([np.zeros(dimension) if h['kind'] == 'constant'
                            else np.asarray(h['weights'])[:, 0] / h['scale'] for h in heads])


def native_fit(data):
    d = data[EMO]
    inner = split(d['groups'])
    heads, selection = protocol.select(d['x'], d['y'], inner)
    w = slopes(heads, d['x'].shape[1])
    u, s, _ = np.linalg.svd(w, full_matrices=False)
    rank = int(np.count_nonzero(s > (s[0] * 1e-10 if s[0] else 0)))
    return {'heads': heads, 'mean': d['x'].mean(0).tolist(), 'basis': u[:, :rank].tolist(),
            'rank': rank, 'singular_values': s.tolist()}, {
        **selection, 'training_ids': d['ids'].tolist(), 'training_groups': d['groups'].tolist(),
        'inner_folds': inner.tolist()}


def representation(native, x):
    raw = raw_heads(native['heads'], x)
    q = np.asarray(native['basis'])
    centered = x - native['mean']
    residual = centered - (centered @ q) @ q.T
    emotion = np.clip(raw, 0, 1)
    return {'emotion': emotion, 'emotion_raw': raw, 'residual': residual,
            'emotion_residual': np.column_stack([emotion, residual])}


def crossfit(data):
    d = data[CAL]
    assignment, result, trace = split(d['groups']), {}, {}
    for fold in range(3):
        valid = assignment == fold
        native, selection = native_fit(protect(data, d['groups'][valid]))
        for method, x in representation(native, d['x'][valid]).items():
            result.setdefault(method, np.empty((len(d['y']), x.shape[1])))[valid] = x
        trace[str(fold)] = {'native': native, 'selection': selection,
                            'validation_groups': d['groups'][valid].tolist()}
    return result, trace


def pca(x):
    mean = x.mean(0)
    _, _, v = np.linalg.svd(x - mean, full_matrices=False)
    return {'mean': mean.tolist(), 'basis': v[:9].T.tolist()}


def project(transform, x):
    return (x - transform['mean']) @ np.asarray(transform['basis'])


def random_projection(dimension):
    q, _ = np.linalg.qr(np.random.default_rng(63730).normal(size=(dimension, 9)))
    return {'mean': np.zeros(dimension).tolist(), 'basis': q.tolist()}


def task_candidates(x, y):
    return candidates(x, y.mean(1, keepdims=True))


def offset_predictions(model, x, offsets):
    raw = raw_heads([model], x)
    return np.clip(raw + np.asarray(offsets)[None, :], 0, 1)


def fit(data):
    d, trace = data[CAL], {'selection': {}}
    inner = split(d['groups'])
    searched = (*MAPS, 'pca9', 'task_pooled', 'task_offset')
    pooled = {m: np.empty((len(d['y']), 6, 2)) for m in searched}
    for fold in range(3):
        valid = inner == fold
        training = protect(data, d['groups'][valid])
        train = training[CAL]
        cross, cross_trace = crossfit(training)
        native, native_trace = native_fit(training)
        held = representation(native, d['x'][valid])
        transform = pca(train['x'])
        cross['pca9'], held['pca9'] = project(transform, train['x']), project(transform, d['x'][valid])
        for method in (*MAPS, 'pca9'):
            for i, model in enumerate(candidates(cross[method], train['y'])):
                pooled[method][valid, i] = native_predict(model, held[method])
        offset = train['y'].mean(0) - train['y'].mean()
        for i, model in enumerate(task_candidates(train['x'], train['y'])):
            pooled['task_pooled'][valid, i] = native_predict(model, d['x'][valid])
            pooled['task_offset'][valid, i] = offset_predictions(model, d['x'][valid], offset)
        trace['selection'][str(fold)] = {'crossfit': cross_trace, 'native': native,
            'native_trace': native_trace, 'pca': transform, 'task_offsets': offset.tolist(),
            'training_groups': train['groups'].tolist(), 'validation_groups': d['groups'][valid].tolist()}
    cross, trace['final_crossfit'] = crossfit(data)
    native, trace['final_native'] = native_fit(data)
    transform = pca(d['x'])
    cross['pca9'] = project(transform, d['x'])
    mappings, trace['map_selection'] = {}, {}
    for method in (*MAPS, 'pca9'):
        loss = ((pooled[method] - d['y'][:, None, :]) ** 2).mean(0)
        selected = loss.argmin(0)
        mappings[method] = selected_heads(candidates(cross[method], d['y']), selected)
        trace['map_selection'][method] = {'selected': selected.tolist(), 'inner_mse': loss.tolist()}
    tasks = {}
    task_models = task_candidates(d['x'], d['y'])
    for method in ('task_pooled', 'task_offset'):
        target = d['y'].mean(1, keepdims=True) if method == 'task_pooled' else d['y']
        loss = ((pooled[method] - target[:, None, :]) ** 2).mean((0, 2))
        selected = int(loss.argmin())
        tasks[method] = protocol.learner.serial(task_models[selected])
        trace['map_selection'][method] = {'selected': selected, 'inner_mse': loss.tolist()}
    direct, trace['direct'] = protocol.select(d['x'], d['y'], inner)
    random = random_projection(d['x'].shape[1])
    random_heads, trace['random9'] = protocol.select(project(random, d['x']), d['y'], inner)
    trace.update(training_groups=d['groups'].tolist(), training_ids=d['ids'].tolist())
    return {'native': native, 'maps': mappings, 'pca': transform, 'direct': direct,
            'random': random, 'random_heads': random_heads, 'tasks': tasks,
            'task_offsets': (d['y'].mean(0) - d['y'].mean()).tolist(),
            'mean': d['y'].mean(0).tolist()}, trace


def predict(model, x, independent=False):
    features = representation(model['native'], x)
    features['pca9'] = project(model['pca'], x)
    result = {m: head_predictions(h, features[m], independent) for m, h in model['maps'].items()}
    result['direct'] = head_predictions(model['direct'], x, independent)
    result['random9'] = head_predictions(model['random_heads'], project(model['random'], x), independent)
    result['mean'] = np.tile(model['mean'], (len(x), 1))
    result['task_pooled'] = np.repeat(head_predictions([model['tasks']['task_pooled']], x, independent), 2, axis=1)
    result['task_offset'] = offset_predictions(model['tasks']['task_offset'], x, model['task_offsets'])
    return result
