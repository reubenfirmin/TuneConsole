"""Independent checks of completed real-data Release artifacts and measurements."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from experiments.audio_lab_tools.test_individual_results import independent_metrics, independent_ridge

ROUND = Path(__file__).resolve().parents[1] / 'audio_lab/local/release-614-20261030/round-2'


def read(path):
    return json.loads(path.read_text())


def ready():
    if not (ROUND / 'evaluation.receipt.json').exists():
        pytest.skip('Completed local Release artifacts are required')


def test_completed_release_receipts_and_transfer_identity_exclusions():
    ready()
    plan = read(ROUND / 'plan.json')
    fingerprint = hashlib.sha256((ROUND / 'plan.json').read_bytes()).hexdigest()
    for section, base in (('inputs', ROUND), ('code', ROUND / 'source')):
        for relative, expected in plan[section].items():
            assert hashlib.sha256((base / relative).read_bytes()).hexdigest() == expected
    for path in ROUND.glob('*.receipt.json'):
        value = read(path)
        assert value['plan_sha256'] == fingerprint
        for relative, expected in value['outputs'].items():
            assert hashlib.sha256((ROUND / relative).read_bytes()).hexdigest() == expected
    metadata = {s: read(ROUND / f'inputs/{s}.json')['rows'] for s in plan['sources']}
    for target in plan['sources']:
        artifact = read(ROUND / f'models/transfer-{target}.json')
        assert target not in artifact['training_ids']
        protected_hashes = {row['audio_sha256'] for row in metadata[target]}
        for source, ids in artifact['training_ids'].items():
            rows = [row for row in metadata[source] if row['id'] in set(ids)]
            assert len(rows) == len(ids)
            assert not {row['audio_sha256'] for row in rows} & protected_hashes
            with np.load(ROUND / f'inputs/{source}.npz') as data:
                expected = {row['id'] for i, row in enumerate(metadata[source]) if data['release_keep'][i]}
                assert set(ids) == expected


def test_completed_release_metrics_recalculate_without_the_reporting_code():
    ready()
    for kind in ('native', 'transfer', 'primary'):
        reports = read(ROUND / f'{kind}.report.json')
        if kind == 'primary':
            reports = {'': reports}
        with np.load(ROUND / f'{kind}.predictions.npz') as data:
            for target, report in reports.items():
                prefix = target + '__' if target else ''
                y, outer = data[prefix + 'observed'], data[prefix + 'outer']
                for method, published in report['metrics'].items():
                    result = independent_metrics(y, data[prefix + method], outer)
                    for metric, expected in zip(('ndcg', 'top_mean', 'top_lift', 'mse'), result, strict=True):
                        assert published[metric] == pytest.approx(expected, abs=1e-12)
                for contrast, values in report['paired'].items():
                    candidate, control = contrast.split('__vs__')
                    for metric in ('ndcg', 'top_lift', 'mse'):
                        expected = report['metrics'][candidate][metric] - report['metrics'][control][metric]
                        assert values[metric]['difference'] == pytest.approx(expected, abs=1e-12)


def test_completed_mapping_traces_match_the_prefit_fold_memberships():
    ready()
    frozen = read(ROUND / 'inputs/folds.json')
    native = read(ROUND / 'native.fit.json')

    def memberships(node):
        return {fold: ({identity for identity, value in zip(node['ids'], node['folds'], strict=True) if value != fold},
                       {identity for identity, value in zip(node['ids'], node['folds'], strict=True) if value == fold})
                for fold in sorted(set(node['folds']))}

    def crossfit(trace, node):
        expected = list(memberships(node).values())
        assert len(trace) == len(expected)
        for actual, (train, valid) in zip(trace, expected, strict=True):
            assert set(actual['train_ids']) == train
            assert set(actual['valid_ids']) == valid

    def mapping(trace, node):
        assert set(trace['anchor_training_ids']) == set(node['ids'])
        assert not set(trace['auxiliary_training_ids']) & set(node['ids'])
        expected = memberships(node)
        assert len(trace['mapping_selection']) == len(expected)
        for actual, (fold, (train, valid)) in zip(trace['mapping_selection'], expected.items(), strict=True):
            assert set(actual['selection_train_ids']) == train
            assert set(actual['selection_valid_ids']) == valid
            crossfit(actual['crossfit'], node['training_subtrees'][str(fold)])
        crossfit(trace['final_crossfit'], node)

    for fold, trace in native.items():
        for source, source_trace in trace['sources'].items():
            node = frozen[source]['outer_training'][fold]
            mapping(source_trace['mapping'], node)
            assert set(trace['native_tasks']['training_ids'][source]) == set(node['ids'])
    for path in ROUND.glob('transfer-*.fit.json'):
        target = path.name.removeprefix('transfer-').removesuffix('.fit.json')
        artifact = read(ROUND / f'models/transfer-{target}.json')
        mapping(read(path)['mapping'], frozen[artifact['anchor_source']]['full'])


def test_completed_primary_predictions_replay_from_serialized_coefficients():
    ready()
    target = 'soundtracks-all'
    artifact = read(ROUND / f'models/transfer-{target}.json')
    with np.load(ROUND / f'inputs/{target}.npz') as data, np.load(ROUND / 'primary.predictions.npz') as outputs:
        for preparation, field in (('historical', 'embedding'), ('browser', 'browser')):
            x = data[field][data['common']]
            mapping = artifact['mapping']
            a = independent_ridge(mapping['anchor'], x)
            b = independent_ridge(mapping['auxiliary'], x)
            for name, weight, calibration in (
                ('monotone', mapping['weight'], mapping['calibration']),
                ('zero_weight', 0, mapping['zero_weight_calibration'])):
                expected = np.clip(calibration['slope'] * ((1 - weight) * a + weight * b) + calibration['intercept'], 0, 1)
                assert np.allclose(expected, outputs[name + '_' + preparation], atol=1e-12, rtol=0)
            joint = artifact['native_tasks']
            column = joint['targets'].index(artifact['anchor_source'])
            coefficients = np.array(joint['shared_weights']) + np.array(joint['deviation_weights'][column])
            raw = [(sum((float(value) - mean) / scale * coefficient for value, mean, scale, coefficient in
                    zip(row, joint['mean'], joint['scale'], coefficients, strict=True)) + joint['intercept'][column]) for row in x]
            assert np.allclose(np.clip(raw, 0, 1), outputs['native_tasks_' + preparation], atol=1e-12, rtol=0)
