"""Post-fit independent replay and arithmetic audit; does not fit or select models."""
from collections import defaultdict
import csv
import io
import json
import math
from pathlib import Path
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from ..support.external_experiment import digest
from .focus_controls_experiment import DEFAULT
from .focus_experiment import arrays, check_receipt, load
from ..studies.individual_experiment import compact, receipt


def raw(head, x):
    if head['kind'] == 'constant':
        return np.tile(head['value'], (len(x), 1))
    assert head['kind'] == 'ridge'
    scale = np.asarray(head['scale'])
    w = np.asarray(head['weights']) / scale[:, None]
    bias = np.asarray(head['intercept']) - np.asarray(head['mean']) @ w
    assert np.isfinite(x).all()
    return x @ w + bias


def heads(h, x):
    return np.column_stack([np.clip(raw(a, x)[:, 0], 0, 1) for a in h])


def replay(model, x):
    native = model['native']
    q = np.asarray(native['basis'])
    eraw = np.column_stack([raw(h, x)[:, 0] for h in native['heads']])
    residual = (x - native['mean']) @ (np.eye(x.shape[1]) - q @ q.T)
    features = {'emotion': np.clip(eraw, 0, 1), 'emotion_raw': eraw, 'residual': residual,
                'emotion_residual': np.column_stack([np.clip(eraw, 0, 1), residual]),
                'pca9': (x - model['pca']['mean']) @ np.asarray(model['pca']['basis'])}
    prediction = {k: heads(h, features[k]) for k, h in model['maps'].items()}
    prediction['direct'] = heads(model['direct'], x)
    prediction['random9'] = heads(model['random_heads'], (x - model['random']['mean']) @ np.asarray(model['random']['basis']))
    prediction['mean'] = np.tile(model['mean'], (len(x), 1))
    prediction['task_pooled'] = np.repeat(np.clip(raw(model['tasks']['task_pooled'], x), 0, 1), 2, axis=1)
    prediction['task_offset'] = np.clip(raw(model['tasks']['task_offset'], x) + model['task_offsets'], 0, 1)
    return prediction


def rank_metrics(y, p):
    k = min(10, max(1, math.ceil(len(y) / 5)))
    discounts = 1 / np.log2(np.arange(k) + 2)
    expected = []
    # Each tied item contributes its block's expected relevance at each rank.
    for score in sorted(set(p), reverse=True):
        mask = p == score
        expected.extend([y[mask].mean()] * int(mask.sum()))
    top = np.array(expected[:k])
    ideal = np.sort(y)[::-1][:k] @ discounts
    return np.array([top @ discounts / ideal, top.mean() - y.mean(), ((p - y) ** 2).mean()])


def listener_replay(d, split, alpha):
    y = d['activities'][:, 3]
    generic = (d['activities'].sum(1) - y) / 8
    out = np.empty((len(y), 3))
    for fold in range(5):
        train = np.flatnonzero(split != fold)
        mu, gm = y[train].mean(), generic[train].mean()
        track, user = defaultdict(list), defaultdict(list)
        for i in train:
            track[int(d['track'][i])].append(i)
            user[int(d['user'][i])].append(i)
        for i in np.flatnonzero(split == fold):
            t, u = track[int(d['track'][i])], user[int(d['user'][i])]
            base = (y[t].sum() + alpha * mu) / (len(t) + alpha)
            out[i, 0] = base
            for c, values, prior in ((1, generic, gm), (2, y, mu)):
                estimate = (values[u].sum() + alpha * prior) / (len(u) + alpha)
                odds = base / (1 - base) * (estimate / (1 - estimate)) / (prior / (1 - prior))
                out[i, c] = odds / (1 + odds)
    return out


def audit(directory=DEFAULT):
    for stage in ('evaluation', 'listeners'):
        check_receipt(directory, stage + '.receipt.json')
    d = arrays(directory / 'inputs/cal500-raw-activity.npz')
    oof = arrays(directory / 'oof.npz')
    evaluation = load(directory / 'evaluation.json')
    maximum = 0.
    for fold in range(5):
        model = load(directory / f'models/outer-{fold}.json')
        valid = d['outer'] == fold
        np.testing.assert_allclose(model['mean'], d['y'][~valid].mean(0), rtol=0, atol=1e-14)
        for prep, x in (('historical', d['x'][valid]), ('browser', d['browser'][valid, 0])):
            for name, p in replay(model, x).items():
                error = float(np.abs(p - oof[prep + '__' + name][valid]).max())
                maximum = max(maximum, error)
                assert error < 1e-10
    common = d['common'].astype(bool)
    metric_error, metric_checks = 0., 0
    for prep, report in evaluation['results'].items():
        for target, column in (('focus', None), ('reading', 0), ('studying', 1)):
            y = d['y'].mean(1) if column is None else d['y'][:, column]
            for name, metrics in report[target]['metrics'].items():
                p = oof[prep + '__' + name]
                p = p.mean(1) if column is None else p[:, column]
                values = np.mean([rank_metrics(y[common & (d['outer'] == f)], p[common & (d['outer'] == f)]) for f in range(5)], axis=0)
                expected = [metrics[k] for k in ('ndcg', 'top_lift', 'mse')]
                metric_error = max(metric_error, float(np.abs(values - expected).max()))
                assert np.allclose(values, expected, rtol=0, atol=1e-12)
                metric_checks += 3
    m = arrays(directory / 'inputs/moods.npz')
    preds = arrays(directory / 'listeners.npz')
    audit = load(directory / 'inputs/audit.json')
    plan = load(directory / 'plan.json')
    archive = next(Path(p) for p in plan['references'] if p.endswith('/moods-activities/Data.zip'))
    with zipfile.ZipFile(archive) as z:
        rows = list(csv.DictReader(io.StringIO(z.read('moods_and_activities_in_music/tags.csv').decode('utf-8-sig'))))
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row['userid'], row['trackid'])].append([int(row['activity.' + a]) for a in audit['moods']['activity_order']])
    expected = np.array([np.mean(buckets[p], axis=0) for p in sorted(buckets)])
    np.testing.assert_array_equal(expected, m['activities'])
    assert len(set(zip(m['user'], m['track']))) == len(expected)
    listener_error = 0.
    for scheme, key in (('artist', 'artist'), ('listener', 'user'), ('pair', None)):
        split = m['fold_' + scheme]
        if key:
            for group in set(m[key]):
                assert len(set(split[m[key] == group])) == 1
        replayed = listener_replay(m, split, 20)
        error = float(np.abs(replayed - preds[scheme + '_20']).max())
        assert error < 1e-12
        listener_error = max(listener_error, error)
    np.testing.assert_allclose(preds['listener_20'][:, 0], preds['listener_20'][:, 2], atol=1e-15, rtol=0)
    result = {'independent_model_replay_max_abs': maximum, 'independent_metric_checks': metric_checks,
        'independent_metric_max_abs': metric_error, 'independent_listener_probability_max_abs': listener_error,
        'moods_raw_rows': len(rows), 'moods_deduplicated_pairs': len(buckets),
        'fold_grouping_and_cold_listener_fallback': 'pass', 'audit_source_sha256': digest(__file__),
        'source': str(Path(__file__).resolve()), 'scope': 'Independent arithmetic and replay; no new fits or model selection.'}
    path = directory / 'independent-audit.json'
    compact(path, result)
    receipt(directory, 'independent-audit', [path])
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    with threadpool_limits(limits=1):
        audit()
