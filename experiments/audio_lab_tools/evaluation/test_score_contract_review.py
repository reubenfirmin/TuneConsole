"""Independent no-fit reconciliation of the frozen #572 calibration results.

Read retained research arrays only; diagnostics go to tmp_path. No extraction,
fitting, bootstrap redraw, reserved-local scoring or application access occurs.
"""
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import pytest

from experiments.audio_lab_tools import source_path
from experiments.audio_lab_tools.studies import test_candidate_followup_review as oracle


EXPERIMENTS = Path(__file__).resolve().parents[2]
DIRECTORY = EXPERIMENTS / 'audio_lab/local/mapped-vibes-20261029/score-contract-1'
PLAN = '10f8685f515e37f3ab1ac5636749a4e4502726ae6f3301dc36ca9a943793b96a'
FIT = '394f74cbb10da124ba9556ade290b18f952a91bb6ec1f663b2a3ffb75205886f'
EVALUATION = '493a6617d8de6c154d688b193dc3f8c1332763a81ebc0bec0b9a3130451922b5'
# The oracle's package/root paths changed; its numerical implementation is unchanged.
ORACLE = 'd80e2b82e7fef0ccb6199c9665ce532062fa3bb04574446b5947ce1dabcba2ef'
SOURCE = 'manymusic-natural'
COHORTS = {SOURCE: (188, 186, 164), 'manymusic-generated': (95, 95, 88),
           'memo2496-pilot': (63, 63, 62)}
CONTROLS = ('checkpoint', 'refresh_calibration', 'refresh_both')
METHODS = (*CONTROLS, 'raw', 'calibrated')


def _affine(model, values):
    assert model['family'] == 'affine'
    assert 0 <= model['slope'] <= 4 and np.isfinite(model['intercept'])
    return np.clip(model['intercept'] + model['slope'] * values, 0, 1)


def _check_metrics(saved, observed, score, split):
    """Reuse the accepted pairwise-position ranking oracle, not the producer."""
    expected = oracle._metrics(observed, score, split)
    bins = []
    for i in range(10):
        left, right = i / 10, (i + 1) / 10
        mask = (score >= left) & ((score < right) if i < 9 else (score <= right))
        if mask.any():
            bins.append({'left': left, 'right': right, 'rows': int(mask.sum()),
                         'mean_score': float(score[mask].mean()),
                         'mean_mapped_rating': float(observed[mask].mean())})
    expected.update(recording_mse=float(np.mean((observed - score) ** 2)),
                    recording_mae=float(np.mean(abs(observed - score))),
                    rating_calibration_gap=sum(b['rows'] * abs(b['mean_score'] - b['mean_mapped_rating'])
                                               for b in bins) / len(score),
                    low_proxy_rows=int((observed <= .3).sum()),
                    high_proxy_rows=int((observed >= .75).sum()),
                    top_low_proxy_fraction=float(np.mean([
                        oracle._fold((observed[split == f] <= .3).astype(float), score[split == f])['top_mean']
                        for f in sorted(set(split))])))
    assert len(saved['rating_bins']) == len(bins)
    for actual, reference in zip(saved['rating_bins'], bins, strict=True):
        oracle._equal(actual, reference)
    oracle._equal({key: value for key, value in saved.items() if key != 'rating_bins'}, expected)
    return {key: expected[key] for key in ('ndcg', 'recording_mse', 'recording_mae',
                                          'rating_calibration_gap', 'top_low_proxy_fraction')}


def test_frozen_affine_calibration_reconciles_actual_results_and_abstention(tmp_path):
    if not DIRECTORY.exists():
        pytest.skip('Separately retained score-contract-1 campaign is unavailable')
    started = time.perf_counter()
    files, checks = {}, {}
    report = {'status': 'incomplete', 'plan_sha256': PLAN, 'checks': checks,
              'runtime': {'python': platform.python_version(), 'numpy': np.__version__},
              'limits': ['Saved affine coefficients are replayed; no model is refitted.',
                         'Existing group/selection diagnostics are checked, not regenerated.',
                         'Conditional bootstrap intervals are inspected, not redrawn.',
                         'Historical research sources are pinned; advancing unexecuted references are recorded without identity claims.',
                         'Only this review test and its independent metric oracle execute numerical Python code.',
                         'No reserved-local labels, raw audio or application data are read.',
                         'No listening-purpose probability or product-adoption claim.']}

    def read(path, expected=None):
        path = Path(path)
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            assert actual == expected, str(path)
        assert files.setdefault(str(path), actual) == actual, str(path)
        return raw

    def load(path, expected=None):
        return json.loads(read(path, expected))

    def arrays(path, expected):
        read(path, expected)
        with np.load(path, allow_pickle=False) as archive:
            return {key: archive[key] for key in archive.files}

    complete = False
    try:
        plan = load(DIRECTORY / 'plan.json', PLAN)
        fit_receipt = load(DIRECTORY / 'fit.receipt.json', FIT)
        evaluation = load(DIRECTORY / 'evaluation.receipt.json', EVALUATION)
        assert fit_receipt['plan_sha256'] == evaluation['plan_sha256'] == PLAN
        assert evaluation['outputs']['fit.receipt.json'] == FIT
        fitted = load(DIRECTORY / 'fit.json', fit_receipt['outputs']['fit.json'])
        source_scores = arrays(DIRECTORY / 'source-predictions.npz', fit_receipt['outputs']['source-predictions.npz'])
        saved = load(DIRECTORY / 'evaluation.report.json', evaluation['outputs']['evaluation.report.json'])
        captures = arrays(DIRECTORY / 'evaluation-predictions.npz', evaluation['outputs']['evaluation-predictions.npz'])
        assert saved['plan_sha256'] == PLAN
        assert plan['created_at'] < fitted['created_at'] < saved['created_at']
        assert not plan['local_labels_opened'] and not plan['target_labels_used_for_fitting']
        assert not fitted['local_labels_used'] and not fitted['target_labels_used']
        assert plan['source'] == fitted['source'] == SOURCE and fitted['source_rows'] == 186
        assert plan['bootstrap_draws'] == 1000 and plan['seed'] == 572029
        assert plan['thresholds'] == [.5, .6, .7, .8, .9, .95]
        current_references = {}
        for relative, expected in plan['code'].items():
            read(DIRECTORY / 'source' / relative, expected)
            product = {'product/audio_embedding.py': 'src/yt_playlist/core/audio_embedding.py',
                       'product/audio_vibe_external.py': 'src/yt_playlist/rec/audio_vibe_external.py',
                       'product/build_runtime.py': 'scripts/audio_model/build_runtime.py'}
            current = EXPERIMENTS.parent / product[relative] if relative in product else EXPERIMENTS / relative
            if relative.startswith('audio_lab_tools/'):
                current = source_path(Path(relative).name)
            read(current)
            current_references[relative] = {'historical_sha256': expected,
                'current_sha256': files[str(current)], 'executed_by_review': False,
                'exact_source_identity': files[str(current)] == expected}
        checks['unexecuted_current_references'] = current_references
        read(Path(__file__))
        oracle_path = Path(oracle.__file__).resolve()
        assert oracle_path == EXPERIMENTS / 'audio_lab_tools/studies/test_candidate_followup_review.py'
        read(oracle_path, ORACLE)
        checks['executed_review_sources'] = {str(path): files[str(path)]
                                            for path in (Path(__file__), oracle_path)}
        cohort_manifest = load(DIRECTORY / 'inputs/cohorts.json', plan['inputs']['inputs/cohorts.json'])
        assert cohort_manifest['cohorts'] == plan['cohorts']
        for audits in cohort_manifest['native_exposure'].values():
            assert set(audits) == set(COHORTS)
            assert all(not audit['excluded_ids'] for audit in audits.values())
        baseline = arrays(DIRECTORY / 'inputs/baseline/external-predictions.npz',
                          plan['inputs']['inputs/baseline/external-predictions.npz'])
        assert set(source_scores) == {'identity', 'mean', 'affine', 'isotonic', 'selected', 'thresholds'}
        assert source_scores['thresholds'].shape == (186,) and np.isnan(source_scores['thresholds']).all()
        assert fitted['final_threshold'] is None
        assert all(not details['passes'] for details in fitted['final_threshold_training'].values())

        summaries, counts, partial_mae, metric_count, expected_capture_keys = {}, {}, {}, 0, set()
        for target, (original_rows, retained_rows, group_count) in COHORTS.items():
            meta = load(DIRECTORY / f'inputs/{target}.json', plan['inputs'][f'inputs/{target}.json'])
            data = arrays(DIRECTORY / f'inputs/{target}.npz', plan['inputs'][f'inputs/{target}.npz'])
            cohort, case = plan['cohorts'][target], saved['cases'][target]
            index = np.asarray(cohort['indices'])
            assert index.dtype.kind in 'iu' and np.array_equal(index, np.unique(index))
            assert len(meta['rows']) == len(meta['groups']) == len(data['y']) == original_rows
            assert len({row['id'] for row in meta['rows']}) == original_rows
            assert (cohort['rows'], cohort['retained_rows'], cohort['groups']) == (original_rows, retained_rows, group_count)
            np.testing.assert_array_equal(data['y'], [row['targets'] for row in meta['rows']])
            np.testing.assert_array_equal(index, np.flatnonzero(baseline[target + '__retained']))
            groups, split = np.asarray(meta['groups'])[index], data['outer'][index]
            assert len(index) == case['rows'] == retained_rows and len(set(groups)) == case['groups'] == group_count
            assert set(split) == set(range(5))
            assert all(len(set(split[groups == group])) == 1 for group in set(groups))
            observed = ((1 - 2 * abs(data['y'][:, meta['target_names'].index('arousal')] - .5)
                         + data['y'][:, meta['target_names'].index('valence')]) / 2)[index]
            np.testing.assert_allclose(baseline[target + '__cruise__observed'][index], observed, atol=1e-15, rtol=0)
            raw = baseline[target + '__cruise__cruise_specialist'][index]

            def transform(values):
                if target != SOURCE:
                    return _affine(fitted['final_model'], values)
                result = np.empty(retained_rows)
                for fold in range(5):
                    mask = split == fold
                    result[mask] = _affine(fitted['outer_models'][str(fold)], values[mask])
                return result

            calibrated = transform(raw)
            if target == SOURCE:
                for name in ('selected', 'affine'):
                    np.testing.assert_allclose(source_scores[name], calibrated, atol=1e-12, rtol=0)
                assert case['outer_thresholds_available'] is False
                for fold, details in fitted['outer'].items():
                    train, test = np.flatnonzero(split != int(fold)), np.flatnonzero(split == int(fold))
                    np.testing.assert_array_equal(details['training_indices'], train)
                    np.testing.assert_array_equal(details['evaluation_indices'], test)
                    assert set(groups[train]).isdisjoint(groups[test])
                    inner = np.asarray(details['inner_fold_for_training_indices'])
                    assert inner.shape == train.shape and set(inner) == {0, 1, 2}
                    assert all(len(set(inner[groups[train] == group])) == 1 for group in set(groups[train]))
                    assert details['family'] == 'affine' and details['threshold'] is None
                    assert all(not value['passes'] for value in details['threshold_training'].values())
                final_inner = np.asarray(fitted['final_inner_split'])
                assert final_inner.shape == split.shape and set(final_inner) == {0, 1, 2}
                assert all(len(set(final_inner[groups == group])) == 1 for group in set(groups))
                for name, entry in case['family_diagnostics'].items():
                    _check_metrics(entry, observed, source_scores[name], split)
                    metric_count += 1
            else:
                assert cohort['source_overlap']['excluded_ids'] == []
            expected_abstention = {'selected_rows': 0, 'selected_groups': 0, 'row_precision': None,
                                  'all_members_high_groups': 0, 'group_precision_lower_95': None, 'passes': False}
            oracle._equal(case['threshold'], expected_abstention)
            scores = {name: baseline[target + '__cruise__' + name][index] for name in CONTROLS}
            scores.update(raw=raw, calibrated=calibrated)
            assert set(case['metrics']) == set(METHODS)
            summaries[target] = {name: _check_metrics(case['metrics'][name], observed, score, split)
                                 for name, score in scores.items()}
            metric_count += len(scores)
            partial_mae[target] = {}
            for label in ('one', 'two', 'all'):
                entry = case['captures'][label]
                assert entry['strong_match_allowed'] is False
                assert len(entry['coverage']) == retained_rows
                values = {name: captures[f'{target}__{label}__{name}'] for name in METHODS}
                expected_capture_keys.update(f'{target}__{label}__{name}' for name in METHODS)
                np.testing.assert_allclose(values['calibrated'], transform(values['raw']), atol=1e-12, rtol=0)
                for name, score in values.items():
                    checked = _check_metrics(entry['metrics'][name], observed, score, split)
                    if name == 'calibrated':
                        partial_mae[target][label] = checked['recording_mae']
                    if label == 'all':
                        np.testing.assert_allclose(score, scores[name], atol=1e-12, rtol=0)
                    metric_count += 1
            for interval in case['paired'].values():
                bounds = np.asarray(interval['conditional_95_interval'])
                assert interval['usable_draws'] == 1000
                assert bounds.shape == (2,) and np.isfinite(bounds).all() and bounds[0] <= bounds[1]
            counts[target] = {'original_rows': original_rows, 'retained_rows': retained_rows, 'groups': group_count}
        assert set(captures) == expected_capture_keys and metric_count == 64
        for variant in ('raw', 'calibrated'):
            passes = (all(summary[variant]['ndcg'] >= summary[control]['ndcg'] + .02
                          and summary[variant]['top_low_proxy_fraction'] <= summary['checkpoint']['top_low_proxy_fraction'] + .05
                          for summary in summaries.values() for control in CONTROLS)
                      and any(saved['cases'][target]['paired'][variant + '_vs_checkpoint_ndcg']['conditional_95_interval'][0] > 0
                              for target in COHORTS if target != SOURCE))
            assert saved['decisions'][variant + '_ranking_candidate'] == passes
        scale = (summaries[SOURCE]['raw']['recording_mse'] - summaries[SOURCE]['calibrated']['recording_mse'] >= .002
                 and all(summary['calibrated']['recording_mse'] <= summary['raw']['recording_mse'] + .005
                         for target, summary in summaries.items() if target != SOURCE)
                 and all(summary['calibrated']['ndcg'] >= summary['raw']['ndcg'] - .02 for summary in summaries.values())
                 and all(partial_mae[target][part] <= summary['calibrated']['recording_mae'] + .05
                         for target, summary in summaries.items() for part in ('one', 'two')))
        assert saved['decisions']['mapped_scale_candidate'] == scale
        assert saved['decisions'] == {'raw_ranking_candidate': True, 'calibrated_ranking_candidate': True,
            'mapped_scale_candidate': True, 'strong_match_candidate': False, 'proxy_threshold_gates': False,
            'strong_match_blockers': ['No direct listening-purpose labels in these external domains.',
                                     'Serving-domain and capture coverage validation remain outstanding.'],
            'product_adoption': False}
        checks.update(cohorts=counts, direct_affine_rows=344, metric_summaries=metric_count,
                      threshold_abstention=True, metrics=summaries)
        complete = True
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        unchanged = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
                     for path, expected in files.items()}
        report.update(status='passed' if complete and all(unchanged.values()) else 'failed_or_incomplete',
                      elapsed_seconds=time.perf_counter() - started, consumed_files=files, unchanged=unchanged)
        (tmp_path / 'score-contract-independent-review.json').write_text(json.dumps(report, indent=2) + '\n')
        assert all(unchanged.values()), unchanged
