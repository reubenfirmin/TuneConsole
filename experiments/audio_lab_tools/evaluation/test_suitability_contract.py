"""Independent score semantics and grouped calibration leakage checks."""
from pathlib import Path
import tempfile

import numpy as np
import pytest

from experiments.audio_lab_tools.evaluation import suitability_calibration as cal
from experiments.audio_lab_tools.evaluation import suitability_experiment as experiment
from experiments.audio_lab_tools.evaluation import vibe_score_contract as contract
from experiments.audio_lab_tools.support.external_experiment import LAB, digest, save


def measurement(value=.9, **kwargs):
    settings = {'intervals': [(10, 20), (20, 30), (30, 40)], 'expected_interval': (10, 40),
                'model': 'model-sha', 'calibrator': 'source-fold-sha', 'domain': 'supported-fixture',
                'domain_supported': True, 'threshold': {'value': .8, 'evidence': 'independent-validation-sha'}}
    settings.update(kwargs)
    return contract.measurement(value, **settings)


def test_cruise_change_leaves_five_scores_and_shared_classification_exact():
    shared = dict(zip(contract.VIBES, (.05, .1, .15, .4, .2, .1), strict=True))
    a = contract.assemble(shared, {'cruise': measurement(.1)})
    b = contract.assemble(shared, {'cruise': measurement(.99), 'relax': measurement(.95)})
    assert a['shared_scores'] == b['shared_scores'] == shared
    assert shared['cruise'] == .15
    assert a['shared_top_vibe'] == b['shared_top_vibe'] == 'energize'
    assert a['strong_vibes'] == []
    assert b['strong_vibes'] == ['relax', 'cruise']
    assert a['independent']['cruise']['strong_match'] is False


@pytest.mark.parametrize(('kwargs', 'reason'), [
    ({'threshold': None}, 'unvalidated_threshold'),
    ({'domain_supported': False}, 'unsupported_domain'),
    ({'intervals': [(10, 20)]}, 'partial_capture'),
    ({'intervals': [(10, 20), (10, 20), (20, 25)]}, 'partial_capture'),
])
def test_high_score_cannot_hide_unsupported_threshold_domain_or_capture(kwargs, reason):
    item = measurement(.99, **kwargs)
    assert item['strong_match'] is None
    assert item['abstention'] == reason
    assert not item['coverage']['whole_recording']


def test_missing_measurement_does_not_become_zero_or_best_available_match():
    item = measurement(None, intervals=[])
    assert item['score'] is None
    assert item['coverage']['seconds'] == 0
    assert item['strong_match'] is None
    assert not item['ranking_allowed']
    with pytest.raises(ValueError, match='without audio'):
        measurement(.99, intervals=[])


def test_overlapping_and_duplicate_intervals_count_union_duration():
    captured = contract.coverage([(0, 15), (10, 20), (0, 15), (20, 30)], (0, 30))
    assert captured['seconds'] == 30
    assert captured['distinct_excerpts'] == 3
    assert captured['complete_annotation_interval']
    with pytest.raises(ValueError, match='outside'):
        contract.coverage([(0, 31)], (0, 30))


def test_partial_replay_reports_actual_intervals_and_rejects_missing_embeddings():
    meta = {'rows': [{'covered_interval': [15, 75],
                     'chunks': [{'start': s, 'seconds': 10} for s in (15, 25, 35, 45, 55, 65)]}]}
    data = {'offsets': np.array([0, 6])}
    assert experiment.capture_coverage(meta, data, [0], 2)[0]['seconds'] == 20
    assert not experiment.capture_coverage(meta, data, [0], 2)[0]['complete_annotation_interval']
    assert experiment.capture_coverage(meta, data, [0], None)[0]['complete_annotation_interval']
    with pytest.raises(ValueError, match='counts differ'):
        experiment.capture_coverage(meta, {'offsets': [0, 7]}, [0], 2)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 1.1, True])
def test_nonfinite_or_non_numeric_scores_are_rejected(bad):
    with pytest.raises(ValueError, match='finite and bounded'):
        measurement(bad)


@pytest.mark.parametrize('family', cal.FAMILIES)
def test_scalar_calibration_is_bounded_monotone_and_handles_constant_input(family):
    x = np.array([.1, .1, .2, .3, .4])
    y = np.array([.8, .4, .2, .9, .7])
    model = cal.fit(family, x, y)
    actual = cal.predict(model, np.linspace(0, 1, 100))
    assert np.isfinite(actual).all()
    assert actual.min() >= 0 and actual.max() <= 1
    assert (np.diff(actual) >= -1e-12).all()
    constant = cal.predict(cal.fit(family, np.full(5, .4), y), np.array([0, .4, 1]))
    assert np.isfinite(constant).all()


def test_isotonic_ties_use_recording_weights_not_unique_score_weights():
    model = cal.fit('isotonic', [.1, .1, .2, .3, .4], [.8, .4, .2, .9, .7])
    assert cal.predict(model, np.array([0, .1, .2, .3, .4, 1])) == pytest.approx([1.4/3, 1.4/3, 1.4/3, .8, .8, .8])


def test_held_out_outcomes_do_not_change_their_own_predictions_or_threshold():
    groups = np.repeat([f'group-{i:02}' for i in range(30)], 2)
    x = np.linspace(.01, .99, len(groups))
    y = np.clip(.2 + .6 * x, 0, 1)
    outer = cal.folds(groups, count=5, seed=cal.SEED)
    before, thresholds_before, details_before, models_before = cal.nested(x, y, groups, outer)
    changed = y.copy()
    changed[outer == 2] = 1 - changed[outer == 2]
    after, thresholds_after, details_after, models_after = cal.nested(x, changed, groups, outer)
    for family in (*cal.FAMILIES, 'selected'):
        assert np.array_equal(before[family][outer == 2], after[family][outer == 2])
    assert np.array_equal(thresholds_before[outer == 2], thresholds_after[outer == 2], equal_nan=True)
    assert details_before['2'] == details_after['2']
    assert models_before[2] == models_after[2]
    for detail in details_before.values():
        training = detail['training_indices']
        assert set(groups[training]).isdisjoint(groups[detail['evaluation_indices']])
        cal.check_groups(groups[training], detail['inner_fold_for_training_indices'])


def test_connected_identity_cannot_cross_outer_folds():
    groups = np.array([f'g-{i}' for i in range(30)])
    outer = np.arange(30) % 5
    groups[1] = groups[0]
    with pytest.raises(ValueError, match='crosses'):
        cal.nested(np.linspace(0, 1, 30), np.linspace(0, 1, 30), groups, outer)


def test_threshold_requires_independent_groups_and_uses_conservative_group_success():
    x, y = np.full(60, .9), np.ones(60)
    threshold, _ = cal.choose_threshold(y, x, ['one-artist'] * 60)
    assert threshold is None
    groups = np.repeat([f'g{i}' for i in range(20)], 3)
    y[::3] = .2
    stats = cal.threshold_stats(y, x, groups, .8)
    assert stats['all_members_high_groups'] == 0
    assert not stats['passes']
    assert cal.choose_threshold(np.ones(60), x, groups)[0] == .5
    assert cal.choose_threshold(np.zeros(60), x, groups)[0] is None
    assert cal.threshold_stats(np.ones(60), x, groups, np.full(60, np.nan))['selected_rows'] == 0


def test_receipts_reject_changed_outputs_and_archive_rejects_changed_inputs():
    local = LAB / 'local'
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='score-contract-test-', dir=local) as temporary:
        directory = Path(temporary).resolve()
        save(directory / 'inputs/example.json', {'controlled_fixture': True})
        save(directory / 'plan.json', {'inputs': {'inputs/example.json': digest(directory / 'inputs/example.json')}, 'code': {}})
        save(directory / 'fit.json', {'fixture': 'fixed'})
        experiment.receipt(directory, 'fit.receipt.json', ('fit.json',))
        experiment.check_receipt(directory, 'fit.receipt.json')
        experiment.validate(directory)
        save(directory / 'fit.json', {'fixture': 'changed'})
        with pytest.raises(ValueError, match='output changed'):
            experiment.check_receipt(directory, 'fit.receipt.json')
        save(directory / 'inputs/example.json', {'controlled_fixture': False})
        with pytest.raises(ValueError, match='input changed'):
            experiment.validate(directory)


def test_fit_stage_completes_with_no_transfer_or_local_outcomes_present():
    local = LAB / 'local'
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='score-contract-fit-test-', dir=local) as temporary:
        directory = Path(temporary).resolve()
        groups = np.repeat([f'controlled-{i}' for i in range(30)], 2)
        x = np.linspace(.1, .9, len(groups))
        y = .2 + .6 * x
        save(directory / f'inputs/{experiment.SOURCE}.json', {'groups': groups.tolist()})
        experiment.npz(directory / f'inputs/{experiment.SOURCE}.npz', outer=cal.folds(groups))
        experiment.npz(directory / 'inputs/baseline/external-predictions.npz', **{
            experiment.SOURCE + '__cruise__cruise_specialist': x,
            experiment.SOURCE + '__cruise__observed': y})
        inputs = {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').rglob('*') if p.is_file()}
        save(directory / 'plan.json', {'inputs': inputs, 'code': {}, 'runtime': experiment.versions(),
             'cohorts': {experiment.SOURCE: {'indices': list(range(len(groups)))}}})
        experiment.fit(directory)
        fitted = experiment.load(directory / 'fit.json')
        assert not fitted['target_labels_used']
        assert not fitted['local_labels_used']
        assert fitted['final_model']['family'] == 'affine'
        experiment.check_receipt(directory, 'fit.receipt.json')


def test_tied_ranking_intrusions_are_order_independent_and_undefined_labels_visible():
    y, score, split = np.array([0., .2, .8, 1.]), np.full(4, .5), np.zeros(4, dtype=int)
    before = experiment.metrics(y, score, split)
    after = experiment.metrics(y[::-1], score, split)
    assert before['top_low_proxy_fraction'] == after['top_low_proxy_fraction'] == .5
    constant = experiment.metrics(np.ones(4), score, split)
    assert constant['low_proxy_rows'] == 0
    assert constant['ndcg'] is None
