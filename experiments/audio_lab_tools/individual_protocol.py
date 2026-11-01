"""Native selection, explicit identity components and paired research metrics."""
import re

import numpy as np

from . import vibe_specialist_models as learner
from .external_training import folds
from .focus_experiment import (calibration, constant, group_guard,
                               head_predictions, native_predict)
from .mapped_vibe_metrics import discounted_gain, summary, top_count
from .transfer_identity import identity_keys

SEED = 530580
FAMILIES = ('embedding', 'attributes', 'combined')
SPECS = [{'family': 'ridge', 'alpha': alpha} for alpha in (.1, 1., 10., 100., 1000.)]


def keys(row, title=False):
    result = identity_keys(row, title)
    if row.get('recording_id'):
        result.add(('recording', row['recording_id']))
    group = row.get('group', '')
    match = re.fullmatch(r'jamendo:artist[_:](\d+)', group)
    if match:
        result.add(('jamendo_artist', str(int(match[1]))))
    match = re.fullmatch(r'track_0*(\d+)', row.get('id', ''))
    if match:
        result.add(('jamendo_track', str(int(match[1]))))
    match = re.search(r'jamendo\.com/track/(\d+)', row.get('source_url', ''))
    if match:
        result.add(('jamendo_track', str(int(match[1]))))
    return result


def components(rows, include_declared=False):
    parents, first = list(range(len(rows))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(rows):
        identifiers = keys(row)
        if include_declared and row.get('group'):
            identifiers.add(('declared', row['group']))
        for key in sorted(identifiers):
            if key in first:
                a, b = root(i), root(first[key])
                parents[max(a, b)] = min(a, b)
            else:
                first[key] = i
    return np.asarray([root(i) for i in range(len(rows))])


def exclude(source, target):
    group = components(source)
    target_keys = set().union(*(keys(row, True) for row in target)) if target else set()
    anchors = {group[i] for i, row in enumerate(source) if keys(row) & target_keys}
    direct = np.asarray([bool(keys(row, True) & target_keys) for row in source])
    keep = np.asarray([not (direct[i] or group[i] in anchors) for i in range(len(source))])
    return keep, {'source_rows': len(source), 'training_rows': int(keep.sum()),
                  'direct_excluded_ids': [row['id'] for i, row in enumerate(source) if direct[i]],
                  'component_additional_ids': [row['id'] for i, row in enumerate(source) if not keep[i] and not direct[i]],
                  'missing_artist_name_rows': sum(not row.get('artist') for row in source)}


def split_inner(groups, outer):
    group_guard(groups, outer)
    result = np.full((len(groups), 6), -1, dtype=int)
    for fold in range(5):
        train = outer != fold
        result[train, fold] = folds(groups[train], count=3, seed=SEED)
        group_guard(groups[train], result[train, fold])
    result[:, 5] = folds(groups, count=3, seed=SEED)
    return result


def select(x, y, split):
    predictions = np.empty((len(y), 1 + len(SPECS), y.shape[1]))
    for fold in sorted(set(split)):
        train, valid = split != fold, split == fold
        models = [constant(y[train], x.shape[1]), *learner.fit_models(x[train], y[train], SPECS)]
        for i, model in enumerate(models):
            predictions[valid, i] = native_predict(model, x[valid])
    loss = np.mean((predictions - y[:, None, :]) ** 2, axis=0)
    selected = loss.argmin(axis=0)
    models = [constant(y, x.shape[1]), *learner.fit_models(x, y, SPECS)]
    heads = []
    for column, index in enumerate(selected):
        model = models[index]
        heads.append(learner.serial({**model, 'value': model['value'][column:column + 1]}
                     if model['kind'] == 'constant' else learner.column_model(model, column)))
    return heads, {'selected': selected.tolist(), 'inner_mse': loss.tolist(),
                   'specs': [{'family': 'mean'}, *SPECS]}


def features(data, family):
    if family == 'combined':
        return np.column_stack([data['attributes'], data['embedding']])
    return data[family]


def mean_chunks(values, offsets, limit=None):
    return np.asarray([values[a:min(b, a + limit) if limit else b].mean(axis=0)
                       for a, b in zip(offsets[:-1], offsets[1:], strict=True)])


def metrics(y, prediction, outer):
    return {**summary(y, prediction, outer), 'reference_mean': float(y.mean()),
            'score_mean': float(prediction.mean()), 'calibration': calibration(y, prediction)}


def brief_metrics(y, prediction, outer):
    values = []
    for fold in sorted(set(outer)):
        observed, score = y[outer == fold], prediction[outer == fold]
        k = top_count(len(observed))
        ideal, _ = discounted_gain(observed, observed, k)
        gain, top = discounted_gain(observed, score, k)
        varying = np.ptp(observed) > 1e-12
        values.append([gain / ideal if varying and ideal > 0 else np.nan,
                       top - observed.mean() if varying else np.nan,
                       np.mean((observed - score) ** 2)])
    values = np.asarray(values)
    return np.asarray([np.nanmean(values[:, c]) if np.isfinite(values[:, c]).any() else np.nan for c in range(3)])


def paired(y, scores, outer, groups, comparisons, draws=1000):
    group_guard(groups, outer)
    groups = np.asarray(groups)
    blocks = [[np.flatnonzero((groups == group) & (outer == fold))
               for group in sorted(set(groups[outer == fold]))] for fold in sorted(set(outer))]
    names = sorted({name for pair in comparisons for name in pair})
    boot = {name: [] for name in names}
    rng = np.random.default_rng(SEED)
    for _ in range(draws):
        sample = np.concatenate([np.concatenate([block[i] for i in rng.integers(len(block), size=len(block))])
                                 for block in blocks])
        for name in names:
            boot[name].append(brief_metrics(y[sample], scores[name][sample], outer[sample]))
    boot = {key: np.asarray(value) for key, value in boot.items()}
    output = {}
    for candidate, control in comparisons:
        point = brief_metrics(y, scores[candidate], outer) - brief_metrics(y, scores[control], outer)
        delta = boot[candidate] - boot[control]
        output[candidate + '__vs__' + control] = {
            metric: {'difference': float(point[c]) if np.isfinite(point[c]) else None,
                     'conditional_95_percent_interval': np.quantile(delta[np.isfinite(delta[:, c]), c], [.025, .975]).tolist()
                         if np.isfinite(delta[:, c]).any() else None,
                     'usable_draws': int(np.isfinite(delta[:, c]).sum()), 'draws': draws}
            for c, metric in enumerate(('ndcg', 'top_lift', 'mse'))}
    return output
