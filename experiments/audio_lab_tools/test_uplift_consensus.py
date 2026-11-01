"""Leakage, source balancing and excerpt checks for the Uplift research."""
import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import uplift_consensus as models
from experiments.audio_lab_tools import uplift_experiment as experiment
from experiments.audio_lab_tools.focus_experiment import arrays, check_receipt, load
from experiments.audio_lab_tools.mapped_vibe_rules import apply, labels


def synthetic():
    rng = np.random.default_rng(618)
    result = {}
    for s in models.SOURCES:
        chunks = rng.normal(size=(36, 3, 4))
        x = chunks.mean(axis=1)
        names = models.NATIVE[s]
        y = np.clip(.5 + x[:, :len(names)] * .15, 0, 1)
        result[s] = {'x': x, 'chunks': chunks, 'counts': np.full(36, 3), 'y': y,
                     'reference': apply(models.expression(s), y, names),
                     'groups': np.repeat(np.arange(18), 2),
                     'ids': np.array([s + ':' + str(i) for i in range(36)])}
    return result


def test_recording_artist_title_and_declared_components_connect_across_sources():
    rows = [{'id': 'a', 'artist': 'One', 'title': 'Song', 'group': 'film:1'},
            {'id': 'b', 'artist': 'Two', 'title': 'Other', 'group': 'film:1'},
            {'id': 'c', 'artist': 'Two', 'title': 'Third'}, {'id': 'local', 'title': 'Song'},
            {'id': 'e', 'audio_sha256': 'abc'}, {'id': 'f', 'audio_sha256': 'abc'}, {'id': 'g'}]
    groups = experiment.global_groups(rows)
    assert len(set(groups[:4])) == 1
    assert groups[4] == groups[5]
    assert len(set(groups)) == 3
    protected = {groups[3]}
    assert [r['id'] for r, g in zip(rows, groups) if g not in protected] == ['e', 'f', 'g']


def test_fixed_and_learned_weights_preserve_valence_without_an_energy_gate():
    for omitted in ((), (models.SOUND,), ('manymusic',), (models.MEMO,)):
        names = [s for s in models.EXPERTS if s not in omitted]
        fixed, grid = models.fixed_weights(names), models.weight_grid(names)
        va = [i for i, s in enumerate(names) if s in models.VALENCE]
        assert fixed[va].sum() == pytest.approx(.5)
        assert fixed.sum() == pytest.approx(1)
        assert np.all(grid >= 0)
        assert np.allclose(grid.sum(axis=1), 1)
        assert np.all(grid[:, va].sum(axis=1) >= .5)
        assert np.all(grid[:, names.index(models.EMO)] <= .25)
        assert grid[0] == pytest.approx([1., *([0.] * (len(names) - 1))])
        assert not any('arousal' in labels(models.expression(s)) for s in models.SOURCES)
        assert not any('energy' in labels(models.expression(s)) for s in models.SOURCES)


def test_repeating_one_source_does_not_increase_its_selection_weight():
    data = synthetic()
    rng = np.random.default_rng(9)
    matrices = {s: rng.uniform(size=(len(d['x']), len(models.EXPERTS))) for s, d in data.items()}
    weights, trace = models.choose_weights(data, matrices, list(models.EXPERTS))
    repeated = {**data, models.MEMO: {k: np.repeat(v, 3, axis=0) for k, v in data[models.MEMO].items()}}
    scores = {**matrices, models.MEMO: np.repeat(matrices[models.MEMO], 3, axis=0)}
    after, changed = models.choose_weights(repeated, scores, list(models.EXPERTS))
    assert after == pytest.approx(weights)
    assert changed['macro_loss'] == pytest.approx(trace['macro_loss'])


def test_native_and_mapping_crossfit_exclude_all_validation_components():
    data = synthetic()
    group_by_id = {i: g for d in data.values() for i, g in zip(d['ids'], d['groups'], strict=True)}
    with threadpool_limits(limits=1):
        model, trace = models.fit(data)
    for step in trace['crossfit'].values():
        valid = {group_by_id[i] for ids in step['validation_ids'].values() for i in ids}
        for native in step['native'].values():
            assert not valid & set(native['training_groups'])
            assigned = {}
            for group, fold in zip(native['training_groups'], native['inner_folds'], strict=True):
                assert assigned.setdefault(group, fold) == fold
    d = data[models.EMO]
    before = models.predict(model, d['chunks'], d['counts'])
    replay = models.predict(model, d['chunks'], d['counts'], True)
    for name in before:
        assert replay[name] == pytest.approx(before[name], abs=1e-12)


def test_clipping_precedes_excerpt_average_and_padding_is_ignored():
    head = {'kind': 'ridge', 'medians': [0.], 'mean': [0.], 'scale': [1.], 'weights': [[1.]], 'intercept': [0.]}
    chunks = np.array([[[-1.], [1.], [999.]], [[.2], [.4], [.6]]])
    prediction = models.native_average([head], chunks, np.array([2, 3]))
    assert prediction[:, 0] == pytest.approx([.5, .4])
    for bad in (np.array([0, 3]), np.array([4, 3]), np.array([1.5, 3])):
        with pytest.raises(ValueError, match='counts'):
            models.native_average([head], chunks, bad)


def test_manymusic_strata_cannot_enter_separately():
    with pytest.raises(ValueError, match='enter or leave'):
        models.expert_matrix({models.NAT: {}}, np.zeros((2, 1, 4)), np.ones(2, dtype=int))


@pytest.mark.skipif(not (experiment.DEFAULT / 'evaluation.receipt.json').exists(), reason='Frozen run not complete')
def test_real_cohorts_have_disjoint_training_and_exact_frozen_references():
    directory = experiment.DEFAULT
    experiment.validate(directory)
    check_receipt(directory, 'evaluation.receipt.json')
    data = experiment.datasets(directory)
    for fold in range(5):
        trace = load(directory / f'outer-{fold}.trace.json')
        valid = set(np.concatenate([d['groups'][d['outer'] == fold] for d in data.values()]))
        for native in trace['final_native'].values():
            assert not valid & set(native['training_groups'])
    for name, omitted in experiment.OMISSIONS.items():
        trace = load(directory / f'transfer-{name}.trace.json')
        protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
        assert not set(omitted) & set(trace['final_native'])
        for native in trace['final_native'].values():
            assert not protected & set(native['training_groups'])
    for source, d in data.items():
        assert np.allclose(d['reference'], apply(models.expression(source), d['y'], models.NATIVE[source]))
    report = load(directory / 'report.json')
    assert report['local']['rows'] == load(directory / 'inputs/audit.json')['local']['usable_rows']
    assert all(np.isfinite(v).all() for k, v in arrays(directory / 'predictions.npz').items() if not k.startswith('ids__'))
