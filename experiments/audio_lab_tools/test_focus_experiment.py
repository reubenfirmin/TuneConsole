"""Independent guard cases for Focus selection, grouping and exported heads."""
import numpy as np
import pytest

from experiments.audio_lab_tools import focus_experiment as focus


def test_connected_group_cannot_cross_outer_folds():
    with pytest.raises(ValueError, match='crosses'):
        focus.group_guard(['collaboration', 'other', 'collaboration'], np.array([0, 1, 1]))


def test_native_selection_replays_each_target_and_preserves_fractional_means():
    rng = np.random.default_rng(576)
    x = rng.normal(size=(36, 4))
    y = np.column_stack([np.full(36, .25), np.full(36, 2 / 3)])
    heads, trace = focus.select_heads(x, y, np.arange(36) % 3)
    assert trace['selected'] == [0, 0]
    expected = np.broadcast_to([.25, 2 / 3], (7, 2))
    np.testing.assert_allclose(focus.head_predictions(heads, x[:7]), expected, atol=1e-12)
    np.testing.assert_allclose(focus.head_predictions(heads, x[:7], True), expected, atol=1e-12)
    assert not np.allclose(expected[:, 0], expected[:, 1])


def test_serialized_heads_are_checked_separately_before_mean():
    x = np.ones((3, 2))
    heads = [focus.learner.serial(focus.constant(np.array([[value]]), 2)) for value in (.2, .8)]
    altered = [{**head, 'value': [value]} for head, value in zip(heads, (.3, .7), strict=True)]
    first = focus.head_predictions(heads, x, True)
    second = focus.head_predictions(altered, x, True)
    np.testing.assert_allclose(first.mean(axis=1), second.mean(axis=1))
    assert np.max(np.abs(first - second)) > .09


def test_bootstrap_metric_matches_independent_tie_expectation():
    y = np.array([0., .5, 1., .5, 0., 1.])
    tied = np.ones(6)
    # k=2. Expected tied top relevance equals the population mean, 1/2.
    ideal = 1 + 1 / np.log2(3)
    expected_ndcg = .5 * (1 + 1 / np.log2(3)) / ideal
    result = focus.brief_metrics(y, tied, np.zeros(6))
    np.testing.assert_allclose(result, [expected_ndcg, 0., np.mean((y - 1) ** 2)])


def test_score_bins_preserve_unknown_empty_bins():
    bins = focus.calibration(np.array([.2, .8]), np.array([.0, 1.]))
    assert bins[1]['rows'] == 0
    assert bins[1]['reference_mean'] is None
    assert bins[4]['reference_mean'] == .8


def test_receipt_checks_old_fingerprint_and_rejects_changed_output(tmp_path):
    focus.save(tmp_path / 'plan.json', {'experiment': 'historical'})
    focus.save(tmp_path / 'artifact.json', {'coefficient': .25})
    focus.save(tmp_path / 'receipt.json', {'fingerprint': focus.digest(tmp_path / 'plan.json'),
               'outputs': {'artifact.json': focus.digest(tmp_path / 'artifact.json')}})
    focus.check_receipt(tmp_path, 'receipt.json')
    focus.save(tmp_path / 'artifact.json', {'coefficient': .5})
    with pytest.raises(ValueError, match='output changed'):
        focus.check_receipt(tmp_path, 'receipt.json')
