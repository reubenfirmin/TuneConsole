"""Independent isolation, arithmetic and reference checks for #616."""
import math

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import energize_consensus as models, energize_experiment as experiment
from experiments.audio_lab_tools.focus_experiment import arrays, check_receipt, load


def synthetic():
    rng = np.random.default_rng(616)
    result = {}
    for source in models.SOURCES:
        chunks = rng.normal(size=(36, 3, 4))
        x = chunks.mean(axis=1)
        y = np.clip(.5 + .15 * x[:, :len(models.NATIVE[source])], 0, 1)
        result[source] = {'x': x, 'chunks': chunks, 'counts': np.full(36, 3), 'y': y,
                          'reference': y.mean(axis=1), 'groups': np.repeat(np.arange(18), 2),
                          'ids': np.array([source + ':' + str(i) for i in range(36)])}
    return result


def scalar(head, x):
    if head['kind'] == 'constant':
        return np.full(len(x), head['value'][0])
    assert head['kind'] == 'ridge'
    result = np.full(len(x), head['intercept'][0])
    for j, coefficient in enumerate(head['weights']):
        result += (x[:, j] - head['mean'][j]) / head['scale'][j] * coefficient[0]
    return np.clip(result, 0, 1)


def independent(model, chunks, counts):
    native = {source: np.array([[scalar(head, row[:count]).mean() for head in value['heads']]
                                for row, count in zip(chunks, counts, strict=True)])
              for source, value in model['experts'].items()}
    activation = [native[s][:, 0] for s in ('deam', 'pmemo', 'memo2496-pilot') if s in native]
    if 'manymusic-natural' in native:
        activation.append((native['manymusic-natural'][:, 0] + native['manymusic-generated'][:, 0]) / 2)
    activation = np.mean(activation, axis=0)
    power, joy = native['emotify-artist'].T
    energy = native['soundtracks-all'][:, 0] if 'soundtracks-all' in native else (power + joy) / 2
    result = {'activation': activation, 'consensus': .5 * activation + .25 * energy + .125 * power + .125 * joy,
              'consensus_no_power': .5 * activation + .25 * energy + .25 * joy,
              'consensus_no_joy': .5 * activation + .25 * energy + .25 * power}
    x = np.column_stack([activation, energy, power, joy])
    for name, mapping in model['activity_maps'].items():
        result[name] = scalar(mapping['head'], x[:, mapping['columns']])
    if 'cal500-raw-activity' in native:
        result['direct_activity'] = native['cal500-raw-activity'][:, 0]
    return result


def test_crossfit_excludes_exercise_components_from_all_native_sources():
    data = synthetic()
    ids = {i: g for d in data.values() for i, g in zip(d['ids'], d['groups'], strict=True)}
    with threadpool_limits(limits=1):
        model, trace = models.fit(data)
    for part in trace['crossfit'].values():
        valid = {ids[i] for i in part['validation_ids']}
        for source, native in part['native'].items():
            assert source != models.CAL
            assert not valid & set(native['training_groups'])
    assert trace['activity']['alpha'] == 100
    d = data[models.CAL]
    for name, values in models.predict(model, d['chunks'], d['counts']).items():
        assert values == pytest.approx(independent(model, d['chunks'], d['counts'])[name], abs=1e-12)


def test_omitted_activity_is_absent_and_manymusic_stays_one_source_family():
    data = synthetic()
    del data[models.CAL]
    with threadpool_limits(limits=1):
        model, _ = models.fit(data)
    d = data[models.PMEMO]
    result = models.predict(model, d['chunks'], d['counts'])
    assert 'activity' not in result and 'direct_activity' not in result
    assert set(model['activity_maps']) == set()
    native = lambda value: {'heads': [{'kind': 'constant', 'value': [value]}]}
    experts = {s: native(0.) for s in (models.DEAM, models.PMEMO, models.MEMO, models.NAT)}
    experts[models.GEN] = native(1.)
    experts[models.EMO] = {'heads': [{'kind': 'constant', 'value': [0.]}, {'kind': 'constant', 'value': [0.]}]}
    assert models.features(experts, np.zeros((1, 1, 4)), np.array([1]))[0, 0] == .125
    del experts[models.NAT]
    with pytest.raises(ValueError, match='together'):
        models.features(experts, np.zeros((1, 1, 4)), np.array([1]))


def test_clip_before_averaging_and_no_valence_input():
    head = {'kind': 'ridge', 'medians': [0.], 'mean': [0.], 'scale': [1.], 'weights': [[1.]], 'intercept': [0.]}
    value = models.native_average([head], np.array([[[-1.], [1.], [999.]]]), np.array([2]))
    assert value[0, 0] == .5
    assert not any('valence' in names for names in models.NATIVE.values())
    assert not any(name.startswith('Usage-') for name in experiment.CAL_ATTRIBUTES)


REAL = pytest.mark.skipif(not (experiment.DEFAULT / 'evaluation.receipt.json').exists(), reason='Frozen experiment not complete')


@REAL
def test_real_components_references_and_semantic_splits_are_disjoint():
    root = experiment.DEFAULT
    experiment.validate(root)
    check_receipt(root, 'evaluation.receipt.json')
    data = experiment.datasets(root)
    ids = {i: g for d in data.values() for i, g in zip(d['ids'], d['groups'], strict=True)}
    for path in (root / 'models').glob('*.json'):
        trace = load(root / (path.stem + '.trace.json'))
        if path.stem.startswith('outer-'):
            fold = int(path.stem[-1])
            protected = set(np.concatenate([d['groups'][d['outer'] == fold] for d in data.values()]))
        elif path.stem.startswith('transfer-'):
            omitted = experiment.OMISSIONS[path.stem.removeprefix('transfer-')]
            protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
            assert not set(omitted) & set(trace['final_native'])
        else:
            protected = set()
        for native in trace['final_native'].values():
            assert not protected & set(native['training_groups'])
        if 'activity' in trace:
            assert not protected & set(trace['activity']['training_groups'])
        for part in trace['crossfit'].values():
            valid = {ids[i] for i in part['validation_ids']}
            for native in part['native'].values():
                assert not (valid | protected) & set(native['training_groups'])
    for part in load(root / 'semantic.trace.json').values():
        protected = set(part['validation_groups'])
        assert not protected & set(part['training_groups'])
        for native in part['native_crossfit'].values():
            assert not (protected | set(native['validation_groups'])) & set(native['training_groups'])
    for source, d in data.items():
        assert np.allclose(d['reference'], d['y'].mean(axis=1))
        assert np.all(np.isfinite(d['y']))
    audit = load(root / 'inputs/audit.json')
    assert audit['cal500_original_annotations']['attributes'] == experiment.CAL_ATTRIBUTES


@REAL
def test_all_saved_outer_and_transfer_predictions_independently_replay():
    root, count = experiment.DEFAULT, 0
    for path in (root / 'models').glob('*.json'):
        if path.stem == 'full':
            continue
        model, saved = load(path), arrays(root / (path.stem + '.npz'))
        for key, values in saved.items():
            if not key.endswith('__ids'):
                continue
            source = key.removesuffix('__ids')
            data = arrays(root / f'inputs/{source}.npz')
            lookup = {name: i for i, name in enumerate(data['ids'])}
            indices = [lookup[i] for i in values]
            for prep, column in (('historical', 'chunks'), ('browser', 'browser')):
                if column not in data:
                    continue
                for method, prediction in independent(model, data[column][indices], data['counts'][indices]).items():
                    assert prediction == pytest.approx(saved[f'{source}__{prep}__{method}'], abs=1e-12)
                    count += len(prediction)
    assert count > 30000


def simple_gain(y, score):
    k = min(10, max(1, math.ceil(.2 * len(y))))
    levels = sorted(set(score), reverse=True)
    ordered = np.concatenate([np.repeat(y[score == level].mean(), np.count_nonzero(score == level)) for level in levels])
    discounts = 1 / np.log2(np.arange(k) + 2)
    return float(ordered[:k] @ discounts), float(ordered[:k].mean())


@REAL
def test_primary_ranking_reports_match_independent_recalculation():
    root = experiment.DEFAULT
    report, saved = load(root / 'report.json'), arrays(root / 'predictions.npz')
    for regime in ('outer', 'transfer'):
        for source in experiment.PRIMARY:
            original = arrays(root / f'inputs/{source}.npz')
            y, folds = original['reference'][original['common']], original['outer'][original['common']]
            for prep, table in report[regime][source].items():
                for name in table['metrics']:
                    score = saved[f'{regime}__{source}__{prep}__{name}']
                    ndcg, lift, mse = [], [], []
                    for fold in sorted(set(folds)):
                        a, b = y[folds == fold], score[folds == fold]
                        ideal, _ = simple_gain(a, a)
                        gain, top = simple_gain(a, b)
                        ndcg.append(gain / ideal)
                        lift.append(top - a.mean())
                        mse.append(np.mean((a - b) ** 2))
                    for metric, values in (('ndcg', ndcg), ('top_lift', lift), ('mse', mse)):
                        assert table['metrics'][name][metric] == pytest.approx(np.mean(values), abs=1e-12)
