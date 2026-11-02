"""Independent likelihood, missingness, gradient and saved-evidence checks."""
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize._numdiff import approx_derivative
from scipy.stats import betabinom, binom

from experiments.audio_lab_tools import focus_counts as m
from experiments.audio_lab_tools.focus_experiment import arrays, load


def test_likelihood_agrees_with_discrete_probability_laws():
    k, n, p = np.array([0, 1, 3, 7]), np.array([2, 3, 5, 7]), np.array([.01, .4, .7, .99])
    np.testing.assert_allclose(m.nll(k, n, p), -binom.logpmf(k, n, p), atol=1e-12)
    for concentration in (1., 4., 19.):
        np.testing.assert_allclose(m.nll(k, n, p, concentration),
            -betabinom.logpmf(k, n, p * concentration, (1 - p) * concentration), atol=1e-12)


@pytest.mark.parametrize('kappa', [None, 1., 4., 19.])
def test_penalized_likelihood_gradient(kappa):
    rng = np.random.default_rng(711)
    z, theta = rng.normal(size=(9, 3)), rng.normal(size=4) * .2
    n, k = np.arange(2, 11), np.arange(9)
    expected = approx_derivative(lambda t: m.objective(t, z, k, n, 10., kappa)[0], theta).ravel()
    np.testing.assert_allclose(m.objective(theta, z, k, n, 10., kappa)[1], expected, rtol=1e-5, atol=1e-6)


def test_unknown_activity_is_missing_and_not_a_negative(tmp_path):
    (tmp_path / 'annotations').mkdir()
    for index, text in enumerate(('Usage-Reading = "Yes"\nUsage-Studying = "Unknown"',
                                 'Usage-Reading = "No"\nUsage-Studying = "Yes"'), 1):
        (tmp_path / f'annotations/song_{index:02d}.txt').write_text(text)
    yes, total, audit = m.reconstruct([{'id': 'cal500:song'}], tmp_path, lambda p: p)
    np.testing.assert_array_equal(yes, [[1, 1]])
    np.testing.assert_array_equal(total, [[2, 1]])
    assert audit['missing_activity_values'] == {'Usage-Studying': 1}


def test_native_unselected_checkbox_preserves_original_task(tmp_path):
    (tmp_path / 'annotations').mkdir()
    (tmp_path / 'annotations/song_01.txt').write_text('PageStatus = "Annotation"\nUsage-Reading = "Yes"')
    yes, total, audit = m.reconstruct([{'id': 'cal500:song'}], tmp_path, lambda p: p)
    np.testing.assert_array_equal(yes, [[1, 0]])
    np.testing.assert_array_equal(total, [[1, 1]])
    assert audit['omitted_unselected_checkboxes']['Usage-Studying'] == 1


def test_completed_counts_and_unbiased_controls():
    directory = Path(__file__).resolve().parents[1] / 'audio_lab/local/focus-counts-711-20261101/round-1'
    if not (directory / 'predictions.npz').exists():
        pytest.skip('Full research evidence not fitted yet')
    d = arrays(directory / 'predictions.npz')
    assert len(d['ids']) == 450
    np.testing.assert_allclose(d['yes'] / d['total'], d['observed'], atol=1e-12)
    report = load(directory / 'report.json')
    for prep in ('historical', 'browser'):
        for method in ('binomial', 'beta_binomial', 'ridge', 'training_mean', 'adopted_full'):
            p = d[prep + '__' + method]
            assert np.isfinite(p).all() and (p >= 0).all() and (p <= 1).all()
            np.testing.assert_allclose(((p - d['observed']) ** 2).mean(0), report['preparations'][prep]['native'][method]['mse'])


def test_completed_fold_isolation_selection_and_ranking_arithmetic():
    """Audit saved models and displayed ranking without calling their evaluator."""
    directory = Path(__file__).resolve().parents[1] / 'audio_lab/local/focus-counts-711-20261101/round-1'
    if not (directory / 'predictions.npz').exists():
        pytest.skip('Full research evidence not fitted yet')
    data = arrays(directory / 'inputs/cal.npz')
    report = load(directory / 'report.json')
    for fold in range(5):
        train, valid = data['outer'] != fold, data['outer'] == fold
        trace = load(directory / f'outer-{fold}.trace.json')
        models = load(directory / f'outer-{fold}.model.json')
        saved = arrays(directory / f'outer-{fold}.npz')
        np.testing.assert_array_equal(trace['training_ids'], data['ids'][train])
        np.testing.assert_array_equal(trace['training_groups'], data['groups'][train])
        np.testing.assert_array_equal(trace['validation_groups'], data['groups'][valid])
        np.testing.assert_array_equal(saved['ids'], data['ids'][valid])
        forbidden = set(data['groups'][valid])
        assert not forbidden.intersection(trace['training_groups'])
        for method in ('binomial', 'beta_binomial'):
            selection, model = trace[method], models[method]
            inner = np.asarray(selection['inner_folds'])
            groups = np.asarray(selection['training_groups'])
            np.testing.assert_array_equal(groups, data['groups'][train])
            assert set(inner) == {0, 1, 2}
            for fit in selection['fits']:
                np.testing.assert_array_equal(fit['training_groups'], groups[inner != fit['fold']])
                np.testing.assert_array_equal(fit['validation_groups'], groups[inner == fit['fold']])
                assert not (forbidden | set(fit['validation_groups'])).intersection(fit['training_groups'])
            chosen = np.asarray(selection['mean_native_nll']).argmin(axis=0)
            np.testing.assert_array_equal(selection['selected'], chosen)
            for task, index in enumerate(chosen):
                alpha, kappa = selection['candidates'][index]
                assert alpha in (1, 10, 100, 1000)
                assert kappa in ((None,) if method == 'binomial' else (1, 4, 19))
                assert model['selected_alpha'][task] == alpha
                assert model['selected_kappa'][task] == kappa
            for prep, x in (('historical', data['x'][valid]), ('browser', data['browser'][valid, 0])):
                z = (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']
                logits = z @ np.asarray(model['weights']) + model['intercept']
                np.testing.assert_allclose(saved[prep + '__' + method], 1 / (1 + np.exp(-logits)), atol=1e-12)
        for prep in ('historical', 'browser'):
            np.testing.assert_allclose(saved[prep + '__training_mean'],
                                       np.broadcast_to(data['y'][train].mean(0), (valid.sum(), 2)))
            prior = arrays(directory / f'inputs/prior-outer-{fold}.npz')
            np.testing.assert_allclose(saved[prep + '__ridge'], prior[prep + '__direct'], atol=1e-12, rtol=0)

    pooled = arrays(directory / 'predictions.npz')
    for prep, result in report['preparations'].items():
        for method, recorded in result['focus'].items():
            metrics = []
            for fold in range(5):
                mask = pooled['outer'] == fold
                y = pooled['observed'][mask].mean(1)
                score = pooled[prep + '__' + method][mask].mean(1)
                k = min(10, max(1, int(np.ceil(.2 * len(y)))))
                ordered_relevance = np.concatenate([
                    np.repeat(y[score == value].mean(), np.count_nonzero(score == value))
                    for value in sorted(set(score), reverse=True)])[:k]
                weights = 1 / np.log2(np.arange(k) + 2)
                ideal = np.sort(y)[::-1][:k] @ weights
                metrics.append((ordered_relevance @ weights / ideal,
                                ordered_relevance.mean() - y.mean(), ((score - y) ** 2).mean()))
            np.testing.assert_allclose(np.mean(metrics, axis=0),
                                       [recorded['ndcg'], recorded['top_lift'], recorded['mse']], atol=1e-12)


def test_completed_original_response_counts_and_listener_audit():
    lab = Path(__file__).resolve().parents[1] / 'audio_lab'
    directory = lab / 'local/focus-counts-711-20261101/round-1'
    if not (directory / 'inputs/counts.npz').exists():
        pytest.skip('Original research evidence not available')
    metadata = load(directory / 'inputs/metadata.json')
    saved = arrays(directory / 'inputs/counts.npz')
    yes = np.zeros_like(saved['yes'])
    totals = np.zeros_like(saved['total'])
    responses, listeners = 0, []
    for index, row in enumerate(metadata['rows']):
        song = row['id'].removeprefix('cal500:')
        paths = sorted((lab / 'local/datasets/cal500/annotations-source/annotations').glob(song + '_[0-9][0-9].txt'))
        assert 2 <= len(paths) <= 14
        for path in paths:
            fields = {}
            for line in path.read_text().splitlines():
                key, separator, value = line.partition(' = ')
                if separator:
                    fields[key] = value.strip().strip('"')
            responses += 1
            if fields.get('uid'):
                listeners.append(fields['uid'])
            for task, key in enumerate(('Usage-Reading', 'Usage-Studying')):
                value = fields.get(key)
                if value == 'Yes':
                    yes[index, task] += 1
                    totals[index, task] += 1
                elif value == 'No' or (value is None and fields.get('PageStatus') == 'Annotation'):
                    totals[index, task] += 1
    assert responses == 1677
    assert len(listeners) == 42 and len(set(listeners)) == 13
    np.testing.assert_array_equal(yes, saved['yes'])
    np.testing.assert_array_equal(totals, saved['total'])
    np.testing.assert_allclose(yes / totals, arrays(directory / 'inputs/cal.npz')['y'], atol=1e-12, rtol=0)
