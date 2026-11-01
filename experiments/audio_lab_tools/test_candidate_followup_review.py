"""Independent numerical review of retained common-target research artifacts.

No fitting, extraction, audio access, bootstrap rerun or product adoption check.
An absent local campaign skips; a present but incomplete campaign fails.
"""
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1] / 'audio_lab/local/mapped-vibes-20261029/common-target-1'
PLAN_SHA256 = 'df733b8cd6014e3077a4ee9e43504105d40dbd1e38676f5c0bc10b8413e5fdae'
PILOTS = {'manymusic-natural': (188, 564), 'manymusic-generated': (95, 285),
          'memo2496-pilot': (63, 378)}
SOURCES = {'pilots': ['pmemo', 'deam', 'emotify-artist'],
           'soundtracks': ['pmemo', 'deam', 'emotify-artist',
                          'jamendo-human-4shards', 'coimbra-mirex-like']}


class _Retained:
    def __init__(self, root):
        self.root, self.hashes, self.checks = root, {}, {}
        self.plan = self.json('plan.json', PLAN_SHA256)

    def read(self, relative, expected):
        path = self.root / relative
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        assert actual == expected, str(path)
        assert self.hashes.setdefault(str(path), actual) == actual
        return raw

    def json(self, relative, expected):
        return json.loads(self.read(relative, expected))

    def input(self, name):
        relative = 'inputs/' + name
        expected = self.plan['inputs'][relative]
        if name.endswith('.json'):
            return self.json(relative, expected)
        self.read(relative, expected)
        with np.load(self.root / relative, allow_pickle=False) as archive:
            return {key: archive[key] for key in archive.files}

    def output(self, stage, name):
        receipt_path = self.root / (stage + '.receipt.json')
        raw = receipt_path.read_bytes()
        receipt_hash = hashlib.sha256(raw).hexdigest()
        assert self.hashes.setdefault(str(receipt_path), receipt_hash) == receipt_hash
        receipt = json.loads(raw)
        assert receipt['state'] == 'complete'
        assert receipt['fingerprint'] == PLAN_SHA256
        expected = receipt['outputs'][name]
        if name.endswith('.json'):
            return self.json(name, expected)
        self.read(name, expected)
        with np.load(self.root / name, allow_pickle=False) as archive:
            return {key: archive[key] for key in archive.files}


@pytest.fixture
def retained_followup(tmp_path):
    if not ROOT.exists():
        pytest.skip('Separately retained common-target-1 local artifacts are unavailable')
    retained = _Retained(ROOT)
    try:
        yield retained
    finally:
        unchanged = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
                     for path, expected in retained.hashes.items()}
        (tmp_path / 'candidate-followup-numerical-review.json').write_text(json.dumps({
            'plan_sha256': PLAN_SHA256, 'checks': retained.checks,
            'files': retained.hashes, 'unchanged': unchanged,
            'limits': ['Retained-array reconciliation, not independent refitting or annotation audit.',
                       'No bootstrap interval recomputation, fresh corpus or listening acceptance.'],
        }, indent=2) + '\n')
        assert all(unchanged.values()), unchanged


def _reference(expression, values, names):
    """Evaluate the frozen annotation formula without the production mapper."""
    if isinstance(expression, str):
        return values[:, names.index(expression)]
    arguments = [_reference(item, values, names) for item in expression['args']]
    operation = expression['op']
    if operation == 'mean':
        return sum(arguments) / len(arguments)
    if operation == 'invert':
        assert len(arguments) == 1
        return 1 - arguments[0]
    if operation == 'middle':
        assert len(arguments) == 1
        return 1 - 2 * abs(arguments[0] - .5)
    assert operation == 'sum', operation
    return np.clip(sum(arguments), 0, 1)


def _ranks(values):
    # Each value's number of predecessors plus its mean position within a tie.
    return (1 + (values[:, None] > values[None, :]).sum(axis=1)
            + ((values[:, None] == values[None, :]).sum(axis=1) - 1) / 2)


def _fold(observed, predicted):
    n = len(observed)
    assert observed.shape == predicted.shape == (n,) and n
    assert np.isfinite(observed).all() and np.isfinite(predicted).all()
    assert ((observed >= 0) & (observed <= 1)).all()
    assert ((predicted >= 0) & (predicted <= 1)).all()
    k = min(10, max(1, math.ceil(n / 5)))
    discounts = 1 / np.log2(np.arange(k) + 2)
    prefix = np.r_[0., np.cumsum(discounts)]
    before = (predicted[:, None] < predicted[None, :]).sum(axis=1)
    ties = (predicted[:, None] == predicted[None, :]).sum(axis=1)
    # Expected position weight per row, rather than production's sorted blocks.
    weights = (prefix[np.minimum(k, before + ties)] - prefix[np.minimum(k, before)]) / ties
    top_weights = (np.minimum(k, before + ties) - np.minimum(k, before)) / (ties * k)
    ideal = float(np.dot(np.sort(observed)[::-1][:k], discounts))
    varying = bool(np.ptp(observed) > 1e-12)
    evaluable = varying and ideal > 0
    mean, top = float(observed.mean()), float(np.dot(observed, top_weights))
    correlation = None
    if varying and np.ptp(predicted) > 1e-12:
        a, b = _ranks(observed), _ranks(predicted)
        a, b = a - a.mean(), b - b.mean()
        correlation = float(np.dot(a, b) / math.sqrt(np.dot(a, a) * np.dot(b, b)))
    return {'rows': n, 'k': k, 'evaluable': varying,
            'ndcg': float(np.dot(observed, weights)) / ideal if evaluable else None,
            'random_ndcg': mean * float(discounts.sum()) / ideal if evaluable else None,
            'top_mean': top, 'random_top_mean': mean,
            'top_lift': top - mean if varying else None, 'spearman': correlation,
            'mse': float(np.mean((observed - predicted) ** 2)),
            'mae': float(np.mean(abs(observed - predicted)))}


def _metrics(observed, predicted, split):
    assert observed.shape == predicted.shape == split.shape
    assert len(np.unique(split)) == 5
    parts = {str(fold): _fold(observed[split == fold], predicted[split == fold])
             for fold in np.unique(split)}
    result = {'folds': parts, 'evaluable_folds': sum(p['evaluable'] for p in parts.values())}
    for name in ('ndcg', 'random_ndcg', 'top_mean', 'random_top_mean',
                 'top_lift', 'spearman', 'mse', 'mae'):
        values = [part[name] for part in parts.values() if part[name] is not None]
        result[name] = sum(values) / len(values) if values else None
    result['ndcg_gain'] = (result['ndcg'] - result['random_ndcg']
                           if result['ndcg'] is not None else None)
    result['folds_above_random'] = sum(p['ndcg'] is not None
                                     and p['ndcg'] > p['random_ndcg'] + 1e-12
                                     for p in parts.values())
    return result


def _equal(actual, expected, location=''):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), location
        for key, value in expected.items():
            _equal(actual[key], value, location + '/' + key)
    elif expected is None or isinstance(expected, (bool, int, str)):
        assert actual == expected, location
    else:
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), location


def test_common_target_arrays_reconcile_references_metrics_and_fixed_ensembles(retained_followup):
    retained = retained_followup
    assert retained.plan['source_sets'] == SOURCES
    assert retained.plan['target_sets'] == {'pilots': list(PILOTS), 'soundtracks': ['soundtracks-all']}
    assert retained.plan['primary_pilot_aggregation'] == 'mean_native_then_map'
    assert retained.plan['local_labels_used'] is False
    assert retained.plan['target_labels_used_for_selection'] is False
    rules = retained.input('rules.json')
    summary = retained.output('summary', 'summary.json')
    assert summary['state'] == 'complete' and summary['plan_sha256'] == PLAN_SHA256
    assert summary['local_labels_used'] is False
    datasets = {}
    for target in (*PILOTS, 'soundtracks-all'):
        metadata, data = retained.input(target + '.json'), retained.input(target + '.npz')
        rows, groups = metadata['rows'], metadata['groups']
        n = PILOTS[target][0] if target in PILOTS else 470
        assert len(rows) == len(groups) == len(data['y']) == len(data['outer']) == n
        assert len({row['id'] for row in rows}) == n
        assert all(len(set(data['outer'][np.asarray(groups) == group])) == 1 for group in set(groups))
        if target in PILOTS:
            assert metadata['target_names'] == ['valence', 'arousal']
            np.testing.assert_array_equal(data['y'], [row['targets'] for row in rows])
            offsets = data['offsets']
            assert offsets.shape == (n + 1,) and offsets[0] == 0
            assert offsets[-1] == len(data['embedding']) == PILOTS[target][1]
            np.testing.assert_array_equal(np.diff(offsets), [len(row['chunks']) for row in rows])
            assert (np.diff(offsets) > 0).all()
            assert all(chunk['seconds'] == 10 for row in rows for chunk in row['chunks'])
        datasets[target] = metadata, data
    directions, numerical_checks, predictions = 0, 0, {}
    for regime, sources in SOURCES.items():
        for source in sources:
            stage = regime + '-' + source
            report = retained.output(stage, stage + '.report.json')
            arrays = retained.output(stage, stage + '.predictions.npz')
            assert report['source'] == source and report['regime'] == regime
            assert report['target_labels_used_for_selection'] is False
            assert summary['sources'][regime][source] == report['targets']
            assert set(report['targets']) == set(retained.plan['target_sets'][regime])
            predictions[stage] = arrays
            for target, target_report in report['targets'].items():
                directions += 1
                metadata, data = datasets[target]
                assert target_report['rows'] == len(data['y'])
                assert target_report['groups'] == len(set(metadata['groups']))
                np.testing.assert_array_equal(arrays[target + '__outer'], data['outer'])
                expected_policies = ({'mean_native_then_map', 'mean_mapped_chunks', 'mean_embedding_then_predict'}
                                     if regime == 'pilots' else {'aligned_excerpt'})
                assert set(target_report['policies']) == expected_policies
                for policy, vibes in target_report['policies'].items():
                    assert vibes and all({'selected', 'training_mean'} <= methods.keys()
                                         for methods in vibes.values())
                    for vibe, methods in vibes.items():
                        observed = _reference(rules[target][vibe]['expression'], data['y'], metadata['target_names'])
                        np.testing.assert_allclose(arrays[target + '__' + vibe + '__observed'],
                                                   observed, rtol=0, atol=1e-15)
                        for method, saved in methods.items():
                            predicted = arrays['__'.join((target, policy, vibe, method))]
                            expected = _metrics(observed, predicted, data['outer'])
                            expected.update(score_mean=float(predicted.mean()), reference_mean=float(observed.mean()))
                            _equal(saved, expected, stage + '/' + target + '/' + policy + '/' + vibe + '/' + method)
                            numerical_checks += 1
    assert directions == 14 and numerical_checks > 0
    ensemble_checks = 0
    assert set(summary['pilot_va_ensembles']) == set(PILOTS)
    for target in PILOTS:
        metadata, data = datasets[target]
        assert set(summary['pilot_va_ensembles'][target]) == {'relax', 'cruise', 'energize', 'uplift'}
        for vibe, saved in summary['pilot_va_ensembles'][target].items():
            key = '__'.join((target, 'mean_native_then_map', vibe, 'selected'))
            observed = _reference(rules[target][vibe]['expression'], data['y'], metadata['target_names'])
            values = (predictions['pilots-pmemo'][key] + predictions['pilots-deam'][key]) / 2
            _equal(saved, _metrics(observed, values, data['outer']), target + '/ensemble/' + vibe)
            ensemble_checks += 1
    assert ensemble_checks == 12
    retained.checks.update(directions=directions, all_method_metric_summaries=numerical_checks,
                           fixed_equal_weight_ensembles=ensemble_checks, pilot_chunks=1227)


def _manual_head(model, features):
    """Direct ridge/RFF algebra, independent of the exported replay implementation."""
    clean = np.where(np.isfinite(features), features, np.asarray(model['medians']))
    standardized = (clean - np.asarray(model['mean'])) / np.asarray(model['scale'])
    if model['kind'] == 'rff':
        transformed = (np.cos(standardized @ np.asarray(model['frequencies']) + np.asarray(model['offsets']))
                       * math.sqrt(2 / model['features']))
        assert model['linear']['kind'] == 'ridge'
        return _manual_head(model['linear'], transformed)
    assert model['kind'] == 'ridge'
    return np.clip(standardized @ np.asarray(model['weights']) + np.asarray(model['intercept']), 0, 1)


def test_prior_focus_export_preserves_exact_heads_and_replays(retained_followup):
    # Only inference is imported as the implementation under test; formulas,
    # references, ranking metrics and loss calculations above remain independent.
    from experiments.audio_lab_tools.mapped_vibe_inference import replay

    retained = retained_followup
    prior = retained.input('prior-focus-heads.json')
    export = retained.output('focus', 'focus-candidate.json')
    report = retained.output('focus', 'focus.report.json')
    for key, value in prior.items():
        assert export[key] == value, key
    assert export['target_names'] == ['Usage-Reading', 'Usage-Studying']
    assert len(export['heads']) == 2
    assert export['vibe'] == 'concentrate' and export['family'] == 'embedding'
    assert export['combine'] == 'equal mean Reading/Studying'
    assert export['plan_sha256'] == PLAN_SHA256
    assert export['local_labels_used'] is False and export['product_adoption'] is False
    source_path = Path(prior['source_path'])
    assert retained.plan['references'][str(source_path)] == prior['source_sha256']
    original = retained.json(str(source_path), prior['source_sha256'])
    for name, head in zip(export['target_names'], export['heads'], strict=True):
        assert head == original['heads'][original['target_names'].index(name)]
    data = retained.input('cal500-raw-activity.npz')
    metadata = retained.input('cal500-raw-activity.json')
    oof = retained.input('cal500-raw-activity.oof.npz')
    assert data['embedding'].shape == (494, 200)
    assert len(metadata['rows']) == len(metadata['groups']) == report['rows'] == 494
    assert len(set(metadata['groups'])) == report['groups'] == 491
    np.testing.assert_array_equal(data['outer'], oof['outer'])
    native_indices = [metadata['target_names'].index(name) for name in export['target_names']]
    observed = data['y'][:, native_indices].mean(axis=1)
    np.testing.assert_allclose(oof['concentrate__observed'], observed, rtol=0, atol=1e-15)
    manual_heads, replayed_heads = [], []
    for head in export['heads']:
        manual = _manual_head(head, data['embedding'])
        replayed = replay(head, data['embedding'])
        assert manual.shape == replayed.shape == (494, 1)
        assert np.isfinite(manual).all() and ((manual >= 0) & (manual <= 1)).all()
        np.testing.assert_allclose(replayed, manual, rtol=0, atol=1e-10)
        manual_heads.append(manual[:, 0])
        replayed_heads.append(replayed[:, 0])
    manual_focus = np.mean(manual_heads, axis=0)
    replayed_focus = np.mean(replayed_heads, axis=0)
    np.testing.assert_allclose(replayed_focus, manual_focus, rtol=0, atol=1e-10)
    methods = {'prior_embedding_mae', 'selected', 'shared', 'native_mse'}
    assert set(report['methods']) == methods
    calculated = {}
    for method in sorted(methods):
        calculated[method] = _metrics(observed, oof['concentrate__' + method], oof['outer'])
        _equal(report['methods'][method], calculated[method], 'focus/' + method)
    assert report['new_model_selected_from_outer_results'] is False
    assert set(report['paired_ndcg_difference_against_prior']) == methods - {'prior_embedding_mae'}
    for method, difference in report['paired_ndcg_difference_against_prior'].items():
        expected = calculated[method]['ndcg'] - calculated['prior_embedding_mae']['ndcg']
        assert difference['point'] == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert report['prior_export_replay_max_absolute_error'] <= 1e-10
    retained.checks.update(focus_rows=494, exact_prior_heads=2, focus_oof_methods=4,
                           independent_replay_max_absolute_error=float(np.max(abs(manual_focus - replayed_focus))),
                           conditional_bootstrap_intervals_recomputed=False)
