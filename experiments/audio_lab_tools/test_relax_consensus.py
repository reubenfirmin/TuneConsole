"""Independent leakage, semantics and balancing checks for #617."""
import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import relax_consensus as models
from experiments.audio_lab_tools import relax_experiment as experiment
from experiments.audio_lab_tools.focus_experiment import arrays, check_receipt, load
from experiments.audio_lab_tools.mapped_vibe_rules import apply, labels


def synthetic():
    rng = np.random.default_rng(617)
    result = {}
    for s in models.SOURCES:
        x = rng.normal(size=(36, 4))
        names = models.NATIVE[s]
        y = np.clip(.5 + x[:, :len(names)] * .15, 0, 1)
        result[s] = {'x': x, 'y': y, 'reference': apply(models.expression(s), y, names),
                     'groups': np.repeat(np.arange(18), 2),
                     'ids': np.array([s + ':' + str(i) for i in range(36)])}
    return result


def test_connected_cross_source_recording_title_and_artist_paths_stay_together():
    rows = [{'id': 'a', 'artist': 'One', 'title': 'Song', 'group': 'film:1'},
            {'id': 'b', 'artist': 'Two', 'title': 'Other', 'group': 'film:1'},
            {'id': 'c', 'artist': 'Two', 'title': 'Third'},
            {'id': 'd', 'title': 'Song'}, {'id': 'e', 'audio_sha256': 'abc'},
            {'id': 'f', 'audio_sha256': 'abc'}, {'id': 'g'}, {'id': 'h'}]
    groups = experiment.global_groups(rows)
    assert len(set(groups[:4])) == 1
    assert groups[4] == groups[5]
    assert len(set(groups)) == 4


def test_consensus_preserves_anchor_and_does_not_require_positive_valence():
    for names in (models.EXPERTS, (models.EMO, models.SOUND, models.MEMO), (models.EMO,)):
        grid = models.weight_grid(names)
        assert np.all(grid >= 0)
        assert np.all(grid[:, 0] >= .5)
        assert np.allclose(grid.sum(axis=1), 1)
        assert grid[0, 0] == 1
        fixed = models.fixed_weights(names)
        assert fixed.sum() == pytest.approx(1)
        # Every auxiliary can be zero and a calm anchor still contributes >= 0.5.
        assert np.all(grid @ np.array([1., *([0.] * (len(names) - 1))]) >= .5)
    assert 'valence' not in labels(models.expression(models.EMO))


def test_corpus_weight_selection_is_invariant_to_repeating_one_source():
    data = synthetic()
    rng = np.random.default_rng(9)
    matrices = {s: rng.uniform(size=(len(d['x']), 4)) for s, d in data.items()}
    weights, trace = models.choose_weights(data, matrices, list(models.EXPERTS))
    repeated = {**data, models.MEMO: {k: np.repeat(v, 3, axis=0) for k, v in data[models.MEMO].items()}}
    scores = {**matrices, models.MEMO: np.repeat(matrices[models.MEMO], 3, axis=0)}
    after, changed = models.choose_weights(repeated, scores, list(models.EXPERTS))
    assert after == pytest.approx(weights)
    assert changed['macro_loss'] == pytest.approx(trace['macro_loss'])


def test_native_heads_and_mapping_crossfit_exclude_all_validation_components():
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
    for source, expert in model['experts'].items():
        assert len(expert['heads']) == len(models.NATIVE[source])
    before = models.predict(model, data[models.EMO]['x'])
    replay = models.predict(model, data[models.EMO]['x'], True)
    for name in before:
        assert replay[name] == pytest.approx(before[name], abs=1e-12)


def test_semantic_diagnostic_cannot_reconstruct_its_target_from_defining_columns():
    for source, attributes in models.DIAGNOSTIC.items():
        assert not labels(models.expression(source)) & set(attributes)
    # VA has no separately observed calmness, so no invented semantic diagnosis.
    assert models.NAT not in models.DIAGNOSTIC
    assert models.MEMO not in models.DIAGNOSTIC


def test_manymusic_strata_must_leave_training_together():
    with pytest.raises(ValueError, match='enter or leave'):
        models.expert_matrix({models.NAT: {'heads': [], 'expression': models.expression(models.NAT),
                                        'target_names': models.NATIVE[models.NAT]}}, np.zeros((2, 4)))


@pytest.mark.skipif(not (experiment.DEFAULT / 'evaluation.receipt.json').exists(), reason='Frozen run not complete')
def test_retained_real_results_have_disjoint_training_and_exact_frozen_references():
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
    assert report['resources']['pass']
    assert report['local']['rows'] == load(directory / 'inputs/audit.json')['local']['usable_rows']
    assert all(np.isfinite(v).all() for k, v in arrays(directory / 'predictions.npz').items() if not k.startswith('ids__'))
