"""Independent constraints on the Release mapping experiment's learning design."""
import numpy as np
import pytest

from experiments.audio_lab_tools import release_models as models
from experiments.audio_lab_tools.release_experiment import ATTRIBUTES, TARGET


def sample(prefix, seed, count=48):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(count, 4))
    return {'x': x, 'y': np.clip(.4 + .15 * x[:, 0] - .08 * x[:, 1], 0, 1),
            'groups': np.repeat(np.arange(count // 2), 2),
            'ids': np.array([prefix + str(i) for i in range(count)])}


def test_native_task_balance_is_invariant_to_duplicate_rows_from_one_source():
    data = {'aggression': sample('a', 1), 'power': sample('b', 2)}
    original = models.tasks_fit(data, .1)
    repeated = {**data, 'power': {k: np.repeat(v, 3, axis=0) for k, v in data['power'].items()}}
    repeated_model = models.tasks_fit(repeated, .1)
    for target in data:
        before = models.tasks_predict(original, data[target]['x'], target)
        after = models.tasks_predict(repeated_model, data[target]['x'], target)
        assert np.allclose(before, after, atol=1e-12, rtol=0)


def test_missing_native_annotations_are_not_negative_targets():
    a, b = sample('a', 1), sample('b', 2)
    a['x'][:] = 0
    b['x'][:] = 0
    a['y'][:] = .2
    b['y'][:] = .8
    model = models.tasks_fit({'aggression': a, 'power': b}, 1.)
    assert models.tasks_predict(model, a['x'], 'aggression') == pytest.approx(np.full(48, .2))
    assert models.tasks_predict(model, b['x'], 'power') == pytest.approx(np.full(48, .8))


def test_shared_deviation_replay_agrees_for_every_native_head():
    data = {'aggression': sample('a', 3), 'power': sample('b', 4), 'anger': sample('c', 5)}
    model = models.tasks_fit(data, .01)
    x = np.vstack([d['x'] for d in data.values()])
    for name in data:
        assert np.allclose(models.tasks_predict(model, x, name),
                           models.independent_tasks_predict(model, x, name), atol=1e-12, rtol=0)


def test_affine_calibration_is_monotone_and_handles_constant_scores():
    x = np.linspace(0, 1, 40)
    calibration = models.affine_fit(x, 1 - x)
    assert calibration['slope'] == 0
    assert np.all(np.diff(models.affine_predict(calibration, x)) >= 0)
    constant = models.affine_fit(np.ones(40), x)
    assert constant['slope'] == 0
    assert constant['intercept'] == pytest.approx(.5)
    assert models.affine_fit(x / 100, x)['slope'] == 4


def test_nested_mapping_never_trains_base_heads_on_its_validation_rows():
    anchor, auxiliary = sample('a', 6), sample('b', 7)
    model, trace = models.mapping_fit(anchor, auxiliary)
    assert model['weight'] in (0., .25, .5, .75, 1.)
    all_ids = set(anchor['ids'])
    for outer in trace['mapping_selection']:
        train, valid = set(outer['selection_train_ids']), set(outer['selection_valid_ids'])
        assert not train & valid
        assert train | valid == all_ids
        for inner in outer['crossfit']:
            fit, heldout = set(inner['train_ids']), set(inner['valid_ids'])
            assert fit | heldout == train
            assert not fit & heldout
            assert not (fit | heldout) & valid
    for inner in trace['final_crossfit']:
        assert not set(inner['train_ids']) & set(inner['valid_ids'])
    assert set(trace['auxiliary_training_ids']).isdisjoint(all_ids)


def test_semantic_diagnostics_do_not_use_the_reference_column():
    for source, target in TARGET.items():
        assert target not in ATTRIBUTES[source]
    assert 'aggressive' not in ATTRIBUTES[next(k for k in TARGET if 'jamendo' in k)]


def test_connected_groups_remain_intact_at_every_nested_depth():
    data = sample('x', 8, 60)
    from experiments.audio_lab_tools.release_experiment import fold_tree
    tree = fold_tree(data)
    group_by_id = dict(zip(data['ids'], data['groups'], strict=True))

    def check(node):
        assigned = {}
        for identity, fold in zip(node['ids'], node['folds'], strict=True):
            group = group_by_id[identity]
            assert assigned.setdefault(group, fold) == fold
        for fold, child in node.get('training_subtrees', {}).items():
            expected = {identity for identity, value in zip(node['ids'], node['folds'], strict=True) if value != int(fold)}
            assert set(child['ids']) == expected
            check(child)

    check(tree)
