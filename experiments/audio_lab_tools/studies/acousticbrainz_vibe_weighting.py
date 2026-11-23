"""Test whether weighting strong specialist scores improves AcousticBrainz picks."""
import argparse
import json
from pathlib import Path
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from ..evaluation import mapped_vibe_metrics as metrics
from ..support.external_experiment import ROOT, digest, save, stamp
from ..training import external_training as ridge

UPPER_QUANTILE = .8
WEIGHTS = {'top_picks': 1., 'weight_2x': 2., 'weight_4x': 4.}


def fit_path(x, y, factor):
    if factor == 1.:
        return ridge.fit_path(x, y)
    z, medians, mean, scale = ridge.standardize(x)
    weights = np.where(y[:, 0] >= np.quantile(y[:, 0], UPPER_QUANTILE), factor, 1.)
    # Keep the total weight fixed so alpha has the same scale across fits.
    weights /= weights.mean()
    center = np.average(z, axis=0, weights=weights)
    target_mean = np.average(y, axis=0, weights=weights)
    root = np.sqrt(weights)[:, None]
    u, singular, vt = np.linalg.svd(root * (z - center), full_matrices=False)
    projected = u.T @ (root * (y - target_mean))
    models = []
    for alpha in ridge.ALPHAS:
        coefficients = (vt.T * (singular / (singular ** 2 + alpha))) @ projected
        models.append({'medians': medians, 'mean': mean, 'scale': scale, 'weights': coefficients,
                       'intercept': target_mean - center @ coefficients, 'alpha': alpha})
    return models


def fit(x, y, groups):
    inner = ridge.folds(groups, count=3, seed=ridge.SEED + 1)
    fitted = {'linear': [], **{method: [] for method in WEIGHTS}}
    for target in y.T:
        column = target[:, None]
        linear_alpha = ridge.choose_alpha(x, column, groups, [0])
        for method, factor in WEIGHTS.items():
            top_scores = np.zeros(len(ridge.ALPHAS))
            errors = np.zeros_like(top_scores)
            for fold in range(3):
                train, valid = inner != fold, inner == fold
                actual, count = target[valid], metrics.top_count(np.count_nonzero(valid))
                for i, model in enumerate(fit_path(x[train], column[train], factor)):
                    score = ridge.predict(model, x[valid])[:, 0]
                    _, top = metrics.discounted_gain(actual, score, count)
                    top_scores[i] += top / 3
                    errors[i] += np.abs(actual - score).sum()
            selected = max(range(len(top_scores)), key=lambda i: (round(top_scores[i], 12), -errors[i]))
            full_models = fit_path(x, column, factor)
            fitted[method].append(full_models[selected])
            if method == 'top_picks':
                fitted['linear'].append(full_models[ridge.ALPHAS.index(linear_alpha)])
    return fitted


def arrays(examples, fields, vibes):
    return (np.array([[row['inputs'].get(field) for field in fields] for row in examples], dtype=float),
            np.array([[row['direct'][vibe] for vibe in vibes] for row in examples]))


def predict(models, x):
    return np.column_stack([ridge.predict(model, x)[:, 0] for model in models])


def alphas(fitted, vibes):
    return {method: dict(zip(vibes, (model['alpha'] for model in models), strict=True))
            for method, models in fitted.items()}


def summarize(examples, y, predictions, split, vibes):
    per_vibe = {}
    for i, vibe in enumerate(vibes):
        target = y[:, i]
        upper = target >= np.quantile(target, UPPER_QUANTILE)
        per_vibe[vibe] = {method: {**metrics.summary(target, score[:, i], split),
                                 'mae': float(np.mean(np.abs(target - score[:, i]))),
                                 'strong_actual': float(target[upper].mean()),
                                 'strong_estimate': float(score[upper, i].mean())}
                         for method, score in predictions.items()}
    average = {method: {key: float(np.mean([value[method][key] for value in per_vibe.values()
                                          if value[method][key] is not None]))
                        for key in ('mae', 'top_mean', 'ndcg')
                        if any(value[method][key] is not None for value in per_vibe.values())}
               for method in predictions}
    held_out = [{'recording_id': row['recording_id'], 'fold': int(split[i]), 'direct': row['direct'],
                 **{method: dict(zip(vibes, score[i].tolist(), strict=True))
                    for method, score in predictions.items()}} for i, row in enumerate(examples)]
    return {'average': average, 'per_vibe': per_vibe, 'held_out': held_out}


def compare(data, fresh):
    examples = data['dataset']['examples']
    recorded = data['comparison']['held_out']
    original = {row['recording_id']: row for row in recorded}
    if len(original) != len(recorded) or set(original) != {row['recording_id'] for row in examples}:
        raise ValueError('Frozen predictions must cover every recording exactly once')
    rows = [original[row['recording_id']] for row in examples]
    vibes = list(data['comparison']['vibes'])
    fields = data['plan']['inputs']['classifiers']
    groups = np.array([row['group'] for row in rows])
    split = np.array([row['fold'] for row in rows])
    if (not np.array_equal(split, ridge.folds(groups))
            or any(row['direct'] != example['direct'] for row, example in zip(rows, examples, strict=True))):
        raise ValueError('Frozen artist splits or specialist scores changed')
    x, y = arrays(examples, fields, vibes)
    fill_in = data['snapshot-replay']['scores']
    predictions = {method: np.empty_like(y) for method in ('linear', *WEIGHTS)}
    predictions['fill_in'] = np.array([[fill_in[str(row['recording_id'])][vibe] for vibe in vibes] for row in rows])
    selected = {}
    for fold in sorted(set(split)):
        train, test = split != fold, split == fold
        fitted = fit(x[train], y[train], groups[train])
        selected[int(fold)] = alphas(fitted, vibes)
        for method, models in fitted.items():
            predictions[method][test] = predict(models, x[test])
    linear = [[row['classifiers'][vibe] for vibe in vibes] for row in rows]
    if not np.allclose(predictions['linear'], linear, rtol=0, atol=1e-12):
        raise ValueError('Unweighted fit failed to reproduce the original linear predictions')
    result = {'same_recordings': summarize(examples, y, predictions, split, vibes), 'selected': selected}
    if fresh and fresh['examples']:
        new = fresh['examples']
        if ({row['recording_id'] for row in new} & set(original)
                or {row['mbid'] for row in new} & {row['mbid'] for row in examples}
                or fresh['specialists'] != data['plan']['specialists']
                or fresh['baseline_id'] != data['plan']['baseline_id']
                or fresh['fill_in_source_sha256'] != data['snapshot-replay']['source_sha256']):
            raise ValueError('Fresh recordings must use new identities and the frozen scoring inputs')
        new_x, new_y = arrays(new, fields, vibes)
        fitted = fit(x, y, groups)
        new_predictions = {method: predict(models, new_x) for method, models in fitted.items()}
        new_predictions['fill_in'] = np.array([[row['fill_in'][vibe] for vibe in vibes] for row in new])
        artists = {artist for row in examples for artist in row['artists']}
        result['fresh'] = {**summarize(new, new_y, new_predictions, np.zeros(len(new), dtype=int), vibes),
                           'selected': alphas(fitted, vibes),
                           'recordings_with_training_artist': sum(bool(artists.intersection(row['artists']))
                                                                  for row in new)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('first_run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fresh', type=Path)
    args = parser.parse_args()
    files = {name: args.first_run / (name + '.json') for name in ('dataset', 'plan', 'comparison', 'snapshot-replay')}
    if args.fresh:
        files['fresh'] = args.fresh
    hashes = {name: digest(path) for name, path in files.items()}
    data = {name: json.loads(path.read_text()) for name, path in files.items()}
    if data['comparison']['dataset_sha256'] != hashes['dataset']:
        raise ValueError('Frozen predictions reference a different dataset')
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(output / 'source.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (Path(__file__), Path(ridge.__file__), Path(metrics.__file__)):
            archive.write(path, path.resolve().relative_to(ROOT))
    save(output / 'plan.json', {'created_at': stamp(), 'input_sha256': hashes, 'quantile': UPPER_QUANTILE,
         'weights_above_quantile': WEIGHTS, 'alphas': ridge.ALPHAS, 'same_outer_folds': True,
         'inner_folds': 3, 'selection': 'Mean direct score of the top fifth (capped at ten) per fold and vibe. '
         'Break ties by MAE. Original linear model uses MAE.', 'source_sha256': digest(output / 'source.zip')})
    with threadpool_limits(limits=1):
        result = compare(data, data.get('fresh'))
    save(output / 'comparison.json', result)
    print(json.dumps({name: result[name]['average'] for name in ('same_recordings', 'fresh') if name in result},
                     indent=2))


if __name__ == '__main__':
    main()
