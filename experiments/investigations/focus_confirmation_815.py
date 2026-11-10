"""Actual-data checks of the #815 source audit and larger protected comparison."""
import hashlib

import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.focus.focus_confirmation import DEFAULT, SOURCES, CHROMA, METHODS
from experiments.audio_lab_tools.focus.focus_experiment import arrays, load
from experiments.audio_lab_tools.studies.individual_protocol import group_guard
from experiments.audio_lab_tools.support.quality_round import verify


def test_source_eligibility_and_native_label_support():
    audit = load(DEFAULT / 'inputs/source-audit.json')
    assert audit['independent_confirmation_rows'] == 0
    h = audit['hummusqa']
    assert (h['questions'], h['tracks'], h['unmatched_tracks']) == (320, 108, 61)
    assert len(h['functional_context']) == 17 and len(h['term_matches']) == 3
    assert len(h['study_work_candidates']) == 1
    assert h['study_work_candidates'][0]['id'] == 'hummusqa:26435'
    assert h['study_work_overlap'][0]['id'] == 'manymusic:25250:32.13334:30.0'
    assert h['study_work_overlap'][0]['matching_keys'] == [['artist', 'celestialaeonproject']]
    for record in audit['acquisition']['records']:
        if record['published_md5']:
            assert record['acquired']
            assert hashlib.md5((SOURCES / record['name']).read_bytes()).hexdigest() == record['published_md5']
    d, counts = arrays(DEFAULT / 'inputs/cal.npz'), arrays(DEFAULT / 'inputs/counts.npz')
    assert len(d['ids']) == 494 and len(set(d['groups'])) == 483
    np.testing.assert_array_equal(counts['yes'] / counts['total'], d['y'])
    assert np.isfinite(d['y']).all() and np.all(counts['total'] >= 2)
    assert load(DEFAULT / 'inputs/count-audit.json')['missing_activity_values'] == {}
    assert load(DEFAULT / 'report.json')['new_memo_used'] == 0


def test_exact_intervals_folds_train_only_preprocessing_and_exported_replay():
    assert verify(DEFAULT, required_stages=['extraction', *[f'outer-{f}' for f in range(5)], 'evaluation'])['receipts'] == 7
    d, c, pooled = arrays(DEFAULT / 'inputs/cal.npz'), arrays(DEFAULT / 'chroma.npz'), arrays(DEFAULT / 'predictions.npz')
    np.testing.assert_array_equal(d['ids'], c['ids'])
    np.testing.assert_array_equal(pooled['ids'], d['ids'])
    assert c['chroma'].shape == (494, 72) and np.isfinite(c['chroma']).all()
    rows = load(DEFAULT / 'inputs/rows.json')
    extraction = load(DEFAULT / 'extraction.json')
    prior = load(CHROMA / 'extraction.json')
    old = {(r['id'], r['start']): r['pcm_sha256'] for r in prior['audit']}
    shared = 0
    for row, result in zip(rows, extraction['rows'], strict=True):
        assert result['id'] == row['id'] and result['start'] == row['sample_start']
        key = result['id'], result['start']
        if key in old:
            assert result['pcm_sha256'] == old[key]
            assert result['old_feature_maximum_error'] <= 1e-12
            shared += 1
    assert shared == 32
    group_guard(d['groups'], d['outer'])
    features = {'ridge': d['x'], 'chroma': c['chroma'], 'combined': np.column_stack([d['x'], c['chroma']])}
    with threadpool_limits(limits=1):
        for fold in range(5):
            train, valid = d['outer'] != fold, d['outer'] == fold
            trace = load(DEFAULT / f'outer-{fold}.trace.json')
            models = load(DEFAULT / f'outer-{fold}.model.json')
            np.testing.assert_array_equal(trace['training_ids'], d['ids'][train])
            np.testing.assert_array_equal(trace['validation_ids'], d['ids'][valid])
            assert not set(trace['training_groups']) & set(trace['validation_groups'])
            group_guard(np.array(trace['training_groups']), np.array(trace['inner_folds']))
            predictions = arrays(DEFAULT / f'outer-{fold}.npz')
            accepted = arrays(DEFAULT / f'inputs/prior-outer-{fold}.npz')
            np.testing.assert_allclose(predictions['ridge'], accepted['historical__direct'], rtol=0, atol=1e-12)
            for method, x in features.items():
                medians = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0 for col in x[train].T])
                clean_train = np.where(np.isfinite(x[train]), x[train], medians)
                scale = clean_train.std(0)
                scale[scale < 1e-9] = 1
                columns = []
                for column, head in enumerate(models[method]):
                    if head['kind'] == 'constant':
                        np.testing.assert_allclose(head['value'], [d['y'][train, column].mean()], rtol=0, atol=1e-14)
                        value = np.full(sum(valid), head['value'][0])
                    else:
                        np.testing.assert_array_equal(head['medians'], medians)
                        np.testing.assert_array_equal(head['mean'], clean_train.mean(0))
                        np.testing.assert_array_equal(head['scale'], scale)
                        clean = np.where(np.isfinite(x[valid]), x[valid], medians)
                        z = (clean - np.array(head['mean'])) / scale
                        value = np.clip(z @ np.array(head['weights']) + np.array(head['intercept']), 0, 1).ravel()
                    columns.append(value)
                np.testing.assert_allclose(np.column_stack(columns), predictions[method], rtol=0, atol=1e-12)
                np.testing.assert_array_equal(pooled[method][valid], predictions[method])


def test_reported_errors_uncertainty_and_decision_are_supported():
    report, plan = load(DEFAULT / 'report.json'), load(DEFAULT / 'plan.json')
    d, p = arrays(DEFAULT / 'inputs/cal.npz'), arrays(DEFAULT / 'predictions.npz')
    masks = {'common450': d['common'], 'all494': np.ones(494, dtype=bool),
        'outside_prior_chroma_components': ~np.isin(d['groups'], plan['protocol']['old_chroma_groups'])}
    for name, mask in masks.items():
        r, y = report['cohorts'][name], d['y'][mask]
        assert r['rows'] == int(mask.sum()) and r['groups'] == len(set(d['groups'][mask]))
        for method in METHODS:
            np.testing.assert_array_equal(r['native'][method]['mse'], np.mean((p[method][mask] - y)**2, axis=0))
            expected = np.mean((p[method][mask].mean(1) - y.mean(1))**2)
            # The report averages per-fold MSE; verify each independently.
            for fold in range(5):
                v = d['outer'][mask] == fold
                observed = np.mean((p[method][mask][v].mean(1) - y[v].mean(1))**2)
                assert abs(r['focus'][method]['folds'][str(fold)]['mse'] - observed) <= 1e-14
            assert np.isfinite(expected)
        for pair in r['paired_focus'].values():
            for metric in pair.values():
                assert metric['draws'] == metric['usable_draws'] == 1000
                assert np.isfinite(metric['conditional_95_percent_interval']).all()
        np.testing.assert_allclose(r['paired_native_mse']['difference'],
            np.mean((p['combined'][mask]-y)**2-(p['ridge'][mask]-y)**2, axis=0), rtol=0, atol=1e-14)
    decision = report['decision']
    r = report['cohorts']['common450']
    pair = r['paired_focus']['combined__vs__ridge']
    exposed = r['paired_focus']['combined__vs__adopted_full']
    expected_checks = [pair['ndcg']['difference'] >= .02,
        pair['top_lift']['difference'] >= .01,
        pair['ndcg']['conditional_95_percent_interval'][0] > 0,
        max(np.array(r['native']['combined']['mse']) - r['native']['ridge']['mse']) <= .01,
        exposed['ndcg']['difference'] >= -.01, exposed['top_lift']['difference'] >= -.01]
    assert list(decision['development_checks'].values()) == expected_checks
    assert decision['development_pass'] == all(decision['development_checks'].values())
    assert not decision['independent_confirmation_pass'] and not decision['productionize']
    assert not report['browser_chroma_parity_established']
