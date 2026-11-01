"""Independent arithmetic, nested identity exclusion and report checks for #615."""
import math

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import focus_mapping as models, focus_mapping_experiment as experiment
from experiments.audio_lab_tools.focus_experiment import arrays, check_receipt, load


def scalar(head, x):
    if head['kind'] == 'constant':
        return np.full(len(x), head['value'][0])
    z = (np.where(np.isfinite(x), x, head['medians']) - head['mean']) / head['scale']
    if head['kind'] == 'rff':
        phi = np.sqrt(2 / head['features']) * np.cos(z @ np.asarray(head['frequencies']) + head['offsets'])
        return scalar({'kind': 'ridge', **head['linear']}, phi)
    assert head['kind'] == 'ridge'
    result = np.full(len(x), head['intercept'][0])
    for j, coefficient in enumerate(head['weights']):
        result += z[:, j] * coefficient[0]
    return np.clip(result, 0, 1)


def native(heads, chunks, counts):
    return np.array([[scalar(head, row[:n]).mean() for head in heads] for row, n in zip(chunks, counts, strict=True)])


def independent(model, chunks, counts):
    values = {s: native(e['heads'], chunks, counts) for s, e in model['experts'].items()}
    va = (values['deam'] + values['pmemo'] + values['memo2496-pilot']) / 4
    va += (values['manymusic-natural'] + values['manymusic-generated']) / 8
    attributes = np.column_stack([values['cal500-raw-activity'], va, values['soundtracks-all'], values['emotify-artist']])
    embeddings = np.array([row[:n].mean(axis=0) for row, n in zip(chunks, counts, strict=True)])
    result = {name: native(model[name], chunks, counts) for name in ('direct', 'latest_historical', 'latest_browser')}
    for name, inputs in (('attributes', attributes), ('hybrid', np.column_stack([embeddings, attributes])),
                         ('cal_only', attributes[:, :10]), ('external_only', attributes[:, 10:])):
        result[name] = np.column_stack([scalar(h, inputs) for h in model['maps'][name]])
    return result, attributes


def assert_trace(trace, forbidden=()):
    forbidden = set(forbidden)
    assert not forbidden & set(trace['training_groups'])

    def check_native(part, excluded):
        for source, item in part.items():
            assert not excluded & set(item['training_groups']), source
            assert len(item['training_ids']) == len(item['training_groups'])
            for g in set(item['training_groups']):
                assert len({f for group, f in zip(item['training_groups'], item['inner_folds'], strict=True) if group == g}) == 1

    check_native(trace['final_native'], forbidden)
    for part in trace['final_crossfit'].values():
        check_native(part['native'], forbidden | set(part['validation_groups']))
    for part in trace['selection'].values():
        excluded = forbidden | set(part['validation_groups'])
        check_native(part['native'], excluded)
        for nested in part['crossfit'].values():
            assert not excluded & set(nested['validation_groups'])
            check_native(nested['native'], excluded | set(nested['validation_groups']))


def test_nested_pipeline_excludes_groups_at_every_stage():
    rng = np.random.default_rng(615)
    data = {}
    for source in models.SOURCES:
        chunks = rng.normal(size=(36, 2, 4))
        x = chunks.mean(axis=1)
        target = np.clip(.4 + x @ rng.normal(size=(4, len(models.NATIVE[source]))) * .08, 0, 1)
        data[source] = {'x': x, 'chunks': chunks, 'counts': np.full(36, 2), 'y': target,
                        'groups': np.repeat(np.arange(18), 2), 'ids': np.array([source + ':' + str(i) for i in range(36)])}
        if source == models.CAL:
            data[source].update(semantic=target, browser=x[:, None], y=target[:, :2] / 2)
    forbidden = [0, 1, 2]
    with threadpool_limits(limits=1):
        model, trace = models.fit(models.protect(data, forbidden))
    assert_trace(trace, forbidden)
    d = data[models.CAL]
    observed, attrs = models.predict(model, d['chunks'], d['counts'])
    expected, expected_attrs = independent(model, d['chunks'], d['counts'])
    assert attrs == pytest.approx(expected_attrs, abs=1e-12)
    for method in observed:
        assert observed[method] == pytest.approx(expected[method], abs=1e-12)
    for method in models.METHODS:
        artifact = models.export(model, method, 'plan', {'dimension': 4})
        assert models.exported_predict(artifact, d['chunks'], d['counts']) == pytest.approx(expected[method], abs=1e-12)


def test_manymusic_has_one_family_weight_and_no_activity_inputs():
    experts = {s: {'heads': [{'kind': 'constant', 'value': [1. if s == 'manymusic-generated' else 0.]}
                            for _ in models.NATIVE[s]]} for s in models.SOURCES}
    values = models.attribute_features(experts, np.zeros((1, 1, 4)), np.ones(1, dtype=int))
    assert values[0, 10:12] == pytest.approx([.125, .125])
    assert not any(k.startswith('Usage-') for k in models.FEATURES)
    assert len(models.FEATURES) == 18
    assert sum(map(len, models.NATIVE.values())) == 26


def test_clips_excerpt_predictions_before_averaging_and_ignores_padding():
    head = {'kind': 'ridge', 'medians': [0.], 'mean': [0.], 'scale': [1.], 'weights': [[1.]], 'intercept': [0.]}
    artifact = {'procedure': 'direct', 'heads': [head, head]}
    assert models.exported_predict(artifact, np.array([[[-1.], [1.], [999.]]]), np.array([2])) == pytest.approx(np.array([[.5, .5]]))


REAL = pytest.mark.skipif(not (experiment.DEFAULT / 'evaluation.receipt.json').exists(), reason='Frozen round not complete')


@REAL
def test_real_inputs_and_every_nested_fit_preserve_identity_exclusions():
    experiment.validate(experiment.DEFAULT)
    data = experiment.datasets(experiment.DEFAULT)
    d = data[models.CAL]
    assert len(d['y']) == 494 and d['common'].sum() == 450
    assert np.isfinite(d['semantic']).all()
    audit = load(experiment.DEFAULT / 'inputs/audit.json')
    assert len(audit['cal500']['old_fold_crossing_components']) == 5
    assert audit['cal500']['responses'] == 1677
    identity = {i: g for source in data.values() for i, g in zip(source['ids'], source['groups'], strict=True)}
    for stage in [*('outer-' + str(f) for f in range(5)), 'full']:
        check_receipt(experiment.DEFAULT, stage + '.receipt.json')
        forbidden = [] if stage == 'full' else d['groups'][d['outer'] == int(stage[-1])]
        trace = load(experiment.DEFAULT / (stage + '.trace.json'))
        assert_trace(trace, forbidden)
        for source, native_trace in trace['final_native'].items():
            assert native_trace['training_groups'] == [identity[i] for i in native_trace['training_ids']]
            expected_ids = data[source]['ids'][~np.isin(data[source]['groups'], forbidden)]
            assert native_trace['training_ids'] == expected_ids.tolist()


@REAL
def test_real_outer_native_and_final_scores_replay_independently():
    d = experiment.datasets(experiment.DEFAULT)[models.CAL]
    with threadpool_limits(limits=1):
        for fold in range(5):
            model = load(experiment.DEFAULT / f'models/outer-{fold}.json')
            predictions = arrays(experiment.DEFAULT / f'outer-{fold}.npz')
            mask = d['outer'] == fold
            for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
                expected, attrs = independent(model, d[key][mask], d['counts'][mask])
                assert predictions[prep + '__native_attributes'] == pytest.approx(attrs, abs=1e-12)
                for method, value in expected.items():
                    assert predictions[prep + '__' + method] == pytest.approx(value, abs=1e-12)
            expected = np.column_stack([scalar(h, d['semantic'][mask]) for h in model['observed']])
            assert predictions['observed_attributes'] == pytest.approx(expected, abs=1e-12)


def independent_metrics(y, scores, outer):
    parts = []
    for fold in sorted(set(outer)):
        target, prediction = y[outer == fold], scores[outer == fold]
        k = min(10, math.ceil(.2 * len(target)))
        gains = []
        for value in sorted(set(prediction), reverse=True):
            gains.extend([target[prediction == value].mean()] * int((prediction == value).sum()))
        discounts = 1 / np.log2(np.arange(k) + 2)
        ndcg = np.dot(gains[:k], discounts) / np.dot(np.sort(target)[::-1][:k], discounts)
        parts.append([ndcg, np.mean(gains[:k]), np.mean(gains[:k]) - target.mean(), np.mean((prediction - target) ** 2)])
    return np.mean(parts, axis=0)


@REAL
def test_real_report_metrics_and_decisions_match_saved_predictions():
    check_receipt(experiment.DEFAULT, 'evaluation.receipt.json')
    p = arrays(experiment.DEFAULT / 'predictions.npz')
    report, plan = load(experiment.DEFAULT / 'report.json'), load(experiment.DEFAULT / 'plan.json')
    for prep, tasks in report['outer'].items():
        for task, table in tasks.items():
            y = p['observed'].mean(axis=1) if task == 'focus' else p['observed'][:, 0 if task == 'reading' else 1]
            for method, metrics in table['metrics'].items():
                expected = independent_metrics(y, p[prep + '__' + task + '__' + method], p['outer'])
                assert [metrics[k] for k in ('ndcg', 'top_mean', 'top_lift', 'mse')] == pytest.approx(expected, abs=1e-12)
    assert experiment.decision(report, plan) == report['decisions']
    assert all(r['replay_error'] <= 1e-12 for r in report['resources'].values())
