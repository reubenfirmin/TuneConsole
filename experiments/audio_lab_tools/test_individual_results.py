"""Independent acceptance using real frozen rows and serialized ridge heads.

Does not import the fitter, inference, mapping or ranking implementations.
"""
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest


ROUND = Path(__file__).resolve().parents[1] / 'audio_lab/local/individual-20261030/remaining-1'


def read(path):
    return json.loads(path.read_text())


def ready():
    if not (ROUND / 'evaluation.receipt.json').exists():
        pytest.skip('The complete local research round is not available')


def test_completed_round_receipts_archive_and_source_component_exclusions():
    ready()
    plan = read(ROUND / 'plan.json')
    fingerprint = hashlib.sha256((ROUND / 'plan.json').read_bytes()).hexdigest()
    for relative, expected in plan['code'].items():
        assert hashlib.sha256((ROUND / 'source' / relative).read_bytes()).hexdigest() == expected
    for relative, expected in plan['inputs'].items():
        assert hashlib.sha256((ROUND / relative).read_bytes()).hexdigest() == expected
    for path in ROUND.glob('*.receipt.json'):
        receipt = read(path)
        assert receipt['plan_sha256'] == fingerprint
        for relative, expected in receipt['outputs'].items():
            assert hashlib.sha256((ROUND / relative).read_bytes()).hexdigest() == expected
    for source in plan['sources']:
        metadata = read(ROUND / f'inputs/{source}.json')
        with np.load(ROUND / f'inputs/{source}.npz') as data:
            groups, keep = np.asarray(metadata['groups']), data['native_keep']
            for group in set(groups[keep]):
                assert len(set(data['outer'][keep & (groups == group)])) == 1
            assert all(np.isfinite(data['y']).all(axis=0))
            for family in plan['representations']:
                model = read(ROUND / f'models/{source}.{family}.json')
                assert model['training_rows'] == int(data['transfer_keep'].sum())
                assert model['plan_sha256'] == fingerprint
                assert len(model['heads']) == len(metadata['target_names'])


def independent_metrics(y, scores, split):
    result = []
    for fold in sorted(set(split)):
        observed, predicted = y[split == fold].tolist(), scores[split == fold].tolist()
        k = min(10, math.ceil(len(observed) / 5))
        discounts = [1 / math.log2(position + 2) for position in range(k)]
        ideal = sum(value * weight for value, weight in zip(sorted(observed, reverse=True), discounts))
        gain, top = 0., 0.
        blocks = {}
        for value, score in zip(observed, predicted, strict=True):
            blocks.setdefault(score, []).append(value)
        preceding = 0
        for score in sorted(blocks, reverse=True):
            values = blocks[score]
            stop = min(k, preceding + len(values))
            if preceding < k:
                gain += sum(values) / len(values) * sum(discounts[preceding:stop])
                top += sum(values) / len(values) * (stop - preceding) / k
            preceding += len(values)
        average = sum(observed) / len(observed)
        varying = max(observed) - min(observed) > 1e-12
        result.append([gain / ideal if varying else np.nan, top,
                       top - average if varying else np.nan,
                       sum((a - b) ** 2 for a, b in zip(observed, predicted, strict=True)) / len(observed)])
    return np.nanmean(result, axis=0)


def test_all_primary_results_and_paired_point_estimates_match_saved_rows():
    ready()
    reports = read(ROUND / 'primary.report.json')
    with np.load(ROUND / 'primary.predictions.npz') as data:
        for target, report in reports.items():
            for vibe, case in report['vibes'].items():
                prefix = target + '__' + vibe + '__'
                y, split = data[prefix + 'observed'], data[prefix + 'outer']
                assert len(y) == report['rows']
                for name, published in case['metrics'].items():
                    values = independent_metrics(y, data[prefix + name], split)
                    for metric, expected in zip(('ndcg', 'top_mean', 'top_lift', 'mse'), values, strict=True):
                        assert published[metric] == pytest.approx(expected, abs=1e-12)
                for name, contrasts in case['paired'].items():
                    candidate, control = name.split('__vs__')
                    for metric in ('ndcg', 'top_lift', 'mse'):
                        expected = case['metrics'][candidate][metric] - case['metrics'][control][metric]
                        assert contrasts[metric]['difference'] == pytest.approx(expected, abs=1e-12)


def independent_ridge(head, x):
    if head['kind'] == 'constant':
        return np.repeat(head['value'][0], len(x))
    assert head['kind'] == 'ridge'
    weights = np.asarray(head['weights'])[:, 0]
    values = []
    for row in x:
        value = head['intercept'][0]
        for column, coefficient in enumerate(weights):
            feature = row[column] if math.isfinite(row[column]) else head['medians'][column]
            value += coefficient * (feature - head['mean'][column]) / head['scale'][column]
        values.append(min(1., max(0., value)))
    return np.asarray(values)


def independent_mapping(source, vibe, values, names):
    get = lambda name: values[:, names.index(name)]
    if source in ('pmemo', 'deam'):
        return {'energize': lambda: get('arousal'), 'uplift': lambda: get('valence'),
                'relax': lambda: (1 - get('arousal') + get('valence')) / 2}[vibe]()
    return {'release': lambda: get('power'),
            'energize': lambda: (get('power') + get('joyful_activation')) / 2,
            'relax': lambda: (get('calmness') + 1 - get('tension')) / 2,
            'uplift': lambda: get('joyful_activation')}[vibe]()


def test_primary_embedding_predictions_replay_from_native_coefficients():
    ready()
    reports = read(ROUND / 'primary.report.json')
    with np.load(ROUND / 'primary.predictions.npz') as predicted:
        for target, report in reports.items():
            with np.load(ROUND / f'inputs/{target}.npz') as inputs:
                for source in ('pmemo', 'deam', 'emotify-artist'):
                    model = read(ROUND / f'models/{source}.embedding.json')
                    for preparation, field in (('embedding', 'embedding'), ('browser', 'browser')):
                        native = np.column_stack([independent_ridge(head, inputs[field]) for head in model['heads']])
                        offsets = inputs['offsets']
                        pooled = np.asarray([native[left:right].mean(axis=0) for left, right in zip(offsets[:-1], offsets[1:], strict=True)])
                        for vibe in report['vibes']:
                            if vibe == 'release' and source != 'emotify-artist':
                                continue
                            expected = independent_mapping(source, vibe, pooled, model['target_names'])[inputs['common']]
                            key = target + '__' + vibe + '__' + source + '_' + preparation
                            assert np.allclose(predicted[key], expected, atol=1e-12, rtol=0)
