"""Independent arithmetic and held-out-label perturbation for research acceptance."""
import hashlib
import json
import math

import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.chroma_study import DEFAULT as CHROMA, LAB
from experiments.audio_lab_tools import midlevel_pilot


def read(path):
    return json.loads(path.read_text())


def tied_top(y, scores, k):
    # Compute expected gain by score groups, including entire boundary ties.
    weight = 1 / np.log2(np.arange(k) + 2)
    rank = 0
    gain = top = 0.
    for score in sorted(set(scores), reverse=True):
        members = y[scores == score]
        occupied = min(len(members), k - rank)
        if occupied <= 0:
            break
        gain += float(members.mean()) * float(weight[rank:rank + occupied].sum())
        top += float(members.mean()) * occupied
        rank += occupied
    return gain, top / k


def test_focus_report_arithmetic_and_conditional_diagnostic():
    rows = read(CHROMA / 'extraction.json')['rows']
    ids = np.array([i for i, row in enumerate(rows) if row['source'] == 'cal500-raw-activity'])
    outer = np.array([rows[i]['outer'] for i in ids])
    report = read(CHROMA / 'report.json')['tables']['oof__cal500-raw-activity__focus']
    with np.load(CHROMA / 'predictions.npz') as arrays:
        truth = arrays['truth__cal500-raw-activity'].mean(axis=1)
        for variant in ('musicnn', 'chroma', 'combined'):
            score = arrays[variant + '__native__cal500-raw-activity'][ids].mean(axis=1)
            totals = []
            for fold in sorted(set(outer)):
                y, p = truth[outer == fold], score[outer == fold]
                k = min(10, max(1, math.ceil(.2 * len(y))))
                ideal, _ = tied_top(y, y, k)
                gain, top = tied_top(y, p, k)
                values = [gain / ideal, top - float(y.mean()), float(np.mean((y - p)**2))]
                actual = report['metrics'][variant]['folds'][str(fold)]
                np.testing.assert_allclose(values, [actual[key] for key in ('ndcg', 'top_lift', 'mse')], rtol=0, atol=1e-12)
                totals.append(values)
            np.testing.assert_allclose(np.mean(totals, axis=0),
                [report['metrics'][variant][key] for key in ('ndcg', 'top_lift', 'mse')], rtol=0, atol=1e-12)
    diagnostic = read(LAB / 'local/chroma-810-20261101/focus-permutation/report.json')
    observed = report['metrics']['combined']['ndcg'] - report['metrics']['musicnn']['ndcg']
    np.testing.assert_allclose(diagnostic['observed_difference'], observed, rtol=0, atol=1e-12)
    null = np.asarray(diagnostic['null_differences'])
    assert len(null) == 99 and np.isfinite(null).all()
    assert diagnostic['tail_fraction'] == (1 + int(np.sum(null >= observed))) / 100 == .02
    np.testing.assert_allclose(diagnostic['null_quantiles'], np.quantile(null, [.025, .5, .975]))
    assert diagnostic['plan_sha256'] == hashlib.sha256((CHROMA / 'plan.json').read_bytes()).hexdigest()


def test_held_out_midlevel_labels_cannot_change_fitted_outer_models(tmp_path):
    original = midlevel_pilot.DEFAULT
    with np.load(original / 'inputs/data.npz') as archive:
        data = {key: archive[key].copy() for key in archive.files}
    held_out = data['outer'] == 0
    assert held_out.any() and (~held_out).any()
    # Actual cohort with a deliberate label corruption confined to held-out rows.
    # This is a controlled negative check, not a new result from the frozen plan.
    data['y'][held_out] = 1 - data['y'][held_out]
    data['properties'][held_out] = 1 - data['properties'][held_out]
    (tmp_path / 'inputs').mkdir()
    np.savez_compressed(tmp_path / 'inputs/data.npz', **data)
    (tmp_path / 'plan.json').write_text(json.dumps({
        'purpose': 'Independent held-out label perturbation; not production evaluation',
        'original_plan_sha256': hashlib.sha256((original / 'plan.json').read_bytes()).hexdigest(),
        'changed_rows': int(held_out.sum()), 'changed_fields': ['y', 'properties'], 'outer_fold': 0}))
    with threadpool_limits(limits=1):
        midlevel_pilot.fit(tmp_path)
    for variant in ('direct', 'bottleneck', 'augmented', 'oracle'):
        assert read(tmp_path / f'models/{variant}-0.json') == read(original / f'models/{variant}-0.json')
    with np.load(original / 'predictions.npz') as before, np.load(tmp_path / 'predictions.npz') as after:
        for key in ('direct', 'bottleneck', 'augmented', 'property_prediction', 'property_mean'):
            np.testing.assert_array_equal(after[key][held_out], before[key][held_out])
    # Oracle deliberately consumes test properties; its predictions need not agree.
