"""Independent checks for graded external evaluation and annotation provenance."""
from itertools import permutations
import json

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.studies import mapped_vibe_round as run
from experiments.audio_lab_tools.studies import mapped_vibe_inference as inference
from experiments.audio_lab_tools.evaluation import mapped_vibe_metrics as metric
from experiments.audio_lab_tools.studies.mapped_vibe_rules import ANCHORS, DATASETS, VIBES, apply, registry


def test_focus_uses_fractional_reading_and_studying_votes_directly():
    names = ['Usage-At_work', 'Usage-Reading', 'Usage-Studying']
    y = np.array([[1., 1 / 3, 2 / 3], [0., 0., 1 / 3], [1., 0., 0.]])
    rules = registry()['cal500-raw-activity']
    np.testing.assert_allclose(apply(rules['concentrate']['expression'], y, names), [.5, 1 / 6, 0])
    assert not np.array_equal(apply(rules['concentrate']['expression'], y, names),
                              apply(rules['concentrate_work_ablation']['expression'], y, names))
    with pytest.raises(ValueError, match='missing annotation'):
        apply(rules['concentrate']['expression'], y[:, :2], names[:2])


def test_registry_is_explicit_and_unsupported_pairs_are_absent():
    rules = registry()
    assert set(rules) == set(DATASETS) and len(DATASETS) == 13
    assert sum(len(r) for r in rules.values()) == 53
    for dataset in DATASETS:
        if dataset != 'cal500-raw-activity':
            assert 'concentrate' not in rules[dataset]
    assert 'release' not in rules['pmemo']
    assert 'cruise' not in rules['cal500-raw-activity']
    assert set(ANCHORS) == set(VIBES)
    for vibe, dataset in ANCHORS.items():
        assert vibe in rules[dataset]


def test_tied_ranking_matches_exhaustive_orderings_at_cutoff():
    y = np.array([1., .6, .1, 0.])
    scores = np.array([1., .5, .5, .5])
    gain, top = metric.discounted_gain(y, scores, 2)
    expected_gain, expected_top = [], []
    for order in permutations([1, 2, 3]):
        first = [0, *order][:2]
        expected_gain.append(np.sum(y[first] / np.log2(np.arange(2) + 2)))
        expected_top.append(y[first].mean())
    assert gain == pytest.approx(np.mean(expected_gain))
    assert top == pytest.approx(np.mean(expected_top))
    constant = metric.ranking(y, np.zeros(4))
    assert constant['top_mean'] == pytest.approx(y.mean())
    assert constant['ndcg'] == constant['random_ndcg']
    assert metric.ranking(np.zeros(4), scores)['ndcg'] is None
    assert metric.ranking(np.ones(4), scores)['ndcg'] is None


def test_selection_uses_ranking_then_proper_loss_and_keeps_vote_fractions():
    y = np.array([0., 1 / 3, 2 / 3, 1.] * 3)
    scores = np.column_stack([np.full(12, .5), y, y * .5])
    choice, evidence = metric.choose(y, scores, np.repeat([0, 1, 2], 4))
    assert choice == 1  # identical rankings; correct fractional values win MSE.
    assert evidence['1']['mse'] == 0
    # Constant MSE optimum is the fraction mean, not fraction of nonzero votes.
    model = run.constant(y[:, None], 2, 'mean')
    assert model['value'][0] == .5
    assert model['value'][0] != (y > 0).mean()


@pytest.mark.parametrize('kind', ['ridge', 'rff', 'neighbors'])
def test_export_replay_matches_fractional_models_with_missing_attributes(kind):
    rng = np.random.default_rng(530)
    data = {'attributes': rng.normal(size=(23, 4)), 'embedding': rng.normal(size=(23, 6))}
    data['attributes'][0, 0] = np.nan
    y = rng.integers(0, 4, size=(23, 2)) / 3
    candidates = run.catalogue()
    index = next(i for i, c in enumerate(candidates) if c['features'] == 'attributes_embedding' and c['learner']['family'] == kind)
    with threadpool_limits(limits=1):
        model = run.fit_choices(data, y, [index], candidates)[index]
    serial = json.loads(json.dumps(run.learner.serial(model)))
    x = inference.feature_values(data, 'attributes_embedding')
    np.testing.assert_allclose(inference.replay(serial, x), run.predict_model(model, x), atol=1e-12)
    expert = run.expert_record('fixture', 'concentrate', model, index, candidates, ['Reading', 'Studying'],
                              {'expression': {'op': 'mean', 'args': ['Reading', 'Studying']}}, 'frozen')
    np.testing.assert_allclose(inference.score(expert, data), run.predict_model(model, x).mean(1), atol=1e-12)


def test_heldout_labels_never_enter_selection_and_groups_stay_disjoint():
    rng = np.random.default_rng(531)
    data = {'attributes': rng.normal(size=(30, 3)), 'embedding': rng.normal(size=(30, 5))}
    y = rng.integers(0, 4, size=(30, 2)) / 3
    groups = np.array([f'artist:{i // 2}' for i in range(30)])
    outer = run.folds(groups)
    inner = run.split_arrays(groups, outer)
    candidates = [run.catalogue()[i] for i in (0, 2, 4)]
    train, valid = outer != 0, outer == 0
    assert set(groups[train]).isdisjoint(set(groups[valid]))
    assert (inner[valid, 0] == -1).all()
    for f in range(3):
        assert set(groups[train][inner[train, 0] == f]).isdisjoint(set(groups[train][inner[train, 0] != f]))
    with threadpool_limits(limits=1):
        first = run.pool_oof(run.subset(data, train), y[train], inner[train, 0], candidates)
        y[valid] = 999
        second = run.pool_oof(run.subset(data, train), y[train], inner[train, 0], candidates)
    np.testing.assert_array_equal(first, second)
    assert not np.all(np.isin(first, [0, 1]))


def test_overlap_protection_handles_missing_ids_without_excluding_everything():
    def row(name, artist='', title='', audio=None):
        return {'id': name, 'artist': artist, 'title': title, 'audio_sha256': audio}
    source = [row('1', artist='Artist A'), row('2', title='Sóng Title'), row('3', audio='same'),
              row('4'), row('5'), row('6', artist='Another')]
    target = [row('4'), row('t1', artist='Artist A'), row('t2', title='Song Title'), row('t3', audio='same')]
    assert run.disjoint(source, target).tolist() == [False, False, False, False, True, True]


def test_changing_one_vibe_cannot_change_other_vibe_selection():
    rng = np.random.default_rng(532)
    y, pool = rng.uniform(size=(30, 2)), rng.uniform(size=(30, 3, 2))
    split = np.repeat([0, 1, 2], 10)
    candidates = [run.catalogue()[i] for i in (0, 2, 4)]
    rules = {'relax': {'expression': 'calm'}, 'release': {'expression': 'power'}}
    a = run.selection(y, pool, split, ['calm', 'power'], rules, candidates)
    y[:, 0] = 1 - y[:, 0]
    b = run.selection(y, pool, split, ['calm', 'power'], rules, candidates)
    for method in ('attributes', 'embedding', 'attributes_embedding', 'selected'):
        assert a['vibes']['release'][method] == b['vibes']['release'][method]
