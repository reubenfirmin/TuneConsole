"""Compare nonlinear AcousticBrainz models on the frozen library recordings."""
import argparse
from pathlib import Path
import json
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from ..evaluation import mapped_vibe_metrics
from ..support.external_experiment import ROOT, digest, save, stamp
from ..training import external_learners as learners, external_training as ridge

METHODS = ('rbf', 'neighbours')


def choose_by_vibe(x, y, groups):
    split = ridge.folds(groups, count=3, seed=ridge.SEED + 1)
    losses = {}
    for fold in range(3):
        train, test = split != fold, split == fold
        for key, model in learners.candidates(x[train], y[train]).items():
            if model['kind'] in METHODS:
                error = np.abs(y[test] - learners.prediction(model, x[test])).sum(axis=0)
                losses[key] = losses.get(key, 0.) + error
    return {method: [min((key for key in losses if key.startswith(method + ':')),
                        key=lambda key: losses[key][vibe]) for vibe in range(y.shape[1])]
            for method in METHODS}


def compare(examples, linear, fill_in, fields, vibes):
    previous = {row['recording_id']: row for row in linear}
    if len(previous) != len(examples) or len(previous) != len(linear):
        raise ValueError('Frozen predictions must cover every recording exactly once')
    rows = [previous[example['recording_id']] for example in examples]
    groups = np.array([row['group'] for row in rows])
    split = np.array([row['fold'] for row in rows])
    if (not np.array_equal(split, ridge.folds(groups))
            or any(row['direct'] != example['direct'] for row, example in zip(rows, examples, strict=True))):
        raise ValueError('Frozen artist splits or specialist scores changed')
    x = np.array([[row['inputs'].get(field) for field in fields] for row in examples], dtype=float)
    y = np.array([[row['direct'][vibe] for vibe in vibes] for row in rows])
    predictions = {'fill_in': np.array([[fill_in[str(row['recording_id'])][vibe] for vibe in vibes] for row in rows]),
                   'linear': np.array([[row['classifiers'][vibe] for vibe in vibes] for row in rows]),
                   **{method: np.empty_like(y) for method in METHODS}}
    selected = {}
    for fold in sorted(set(split)):
        train, test = split != fold, split == fold
        choices = choose_by_vibe(x[train], y[train], groups[train])
        models = learners.candidates(x[train], y[train])
        for method, keys in choices.items():
            for index, key in enumerate(keys):
                predictions[method][test, index] = learners.prediction(models[key], x[test])[:, index]
        selected[int(fold)] = {method: dict(zip(vibes, keys, strict=True)) for method, keys in choices.items()}
    per_vibe = {vibe: {method: {**mapped_vibe_metrics.summary(y[:, i], p[:, i], split),
                               'mae': float(np.mean(np.abs(y[:, i] - p[:, i])))}
                       for method, p in predictions.items()} for i, vibe in enumerate(vibes)}
    average = {method: {key: float(np.mean([results[method][key] for results in per_vibe.values()]))
                        for key in ('mae', 'ndcg', 'top_mean')} for method in predictions}
    paired = {method: ridge.bootstrap_improvement(y, predictions['linear'], predictions[method],
                                                  groups, list(range(len(vibes)))) for method in METHODS}
    held_out = [{**row, **{method: dict(zip(vibes, predictions[method][i].tolist(), strict=True))
                          for method in predictions}} for i, row in enumerate(rows)]
    return {'average': average, 'paired_mae_vs_linear': paired, 'per_vibe': per_vibe,
            'selected': selected, 'held_out': held_out}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('first_run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    files = {name: args.first_run / (name + '.json') for name in ('dataset', 'plan', 'comparison', 'snapshot-replay')}
    hashes = {name: digest(path) for name, path in files.items()}
    data = {name: json.loads(path.read_text()) for name, path in files.items()}
    if data['comparison']['dataset_sha256'] != hashes['dataset']:
        raise ValueError('Frozen predictions reference a different dataset')
    fields = data['plan']['inputs']['classifiers']
    vibes = list(data['comparison']['vibes'])
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(output / 'source.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (Path(__file__), *(Path(mod.__file__) for mod in (ridge, learners, mapped_vibe_metrics))):
            archive.write(path, path.resolve().relative_to(ROOT))
    save(output / 'plan.json', {'created_at': stamp(), 'input_sha256': hashes, 'fields': fields,
         'vibes': vibes, 'same_outer_folds': True, 'inner_folds': 3, 'selection': 'MAE per vibe',
         'rbf_gammas': learners.GAMMAS, 'rbf_alphas': learners.KERNEL_ALPHAS,
         'neighbours': learners.NEIGHBOURS, 'neighbour_weights': ['uniform', 'inverse_distance'],
         'source_sha256': digest(output / 'source.zip')})
    with threadpool_limits(limits=1):
        result = compare(data['dataset']['examples'], data['comparison']['held_out'],
                         data['snapshot-replay']['scores'], fields, vibes)
    save(output / 'comparison.json', result)
    print(json.dumps({'average': result['average'], 'paired_mae_vs_linear': result['paired_mae_vs_linear']}, indent=2))


if __name__ == '__main__':
    main()
