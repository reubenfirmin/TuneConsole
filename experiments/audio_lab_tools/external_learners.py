"""Compare linear, radial-basis and neighbour learners on fixed grouped folds."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_training import (ALPHAS, SEED, feature_matrices, fit_path, folds,
                                metrics, predict, standardize)
from .external_transfer import SNAPSHOT, VIBES, external_head_features, score

GAMMAS = (.125, .5, 2., 8.)
KERNEL_ALPHAS = (.001, .01, .1, 1., 10.)
NEIGHBOURS = (3, 7, 15, 31)


def distance(a, b):
    return np.maximum((a * a).sum(axis=1)[:, None] + (b * b).sum(axis=1)[None, :] - 2 * a @ b.T, 0)


def candidates(x, y):
    z, medians, mean, scale = standardize(x)
    preprocessing = {'medians': medians, 'mean': mean, 'scale': scale}
    result = {}
    for model in fit_path(x, y):
        result[f"linear:{model['alpha']}"] = {'kind': 'linear', **model}
    target_mean = y.mean(axis=0)
    squared = distance(z, z) / z.shape[1]
    for gamma in GAMMAS:
        kernel = np.exp(-gamma * squared)
        column_mean, grand_mean = kernel.mean(axis=0), float(kernel.mean())
        centered = kernel - column_mean[None, :] - column_mean[:, None] + grand_mean
        eigenvalues, vectors = np.linalg.eigh(centered)
        projected = vectors.T @ (y - target_mean)
        for alpha in KERNEL_ALPHAS:
            dual = vectors @ (projected / (np.maximum(eigenvalues, 0) + alpha)[:, None])
            result[f'rbf:{gamma}:{alpha}'] = {
                'kind': 'rbf', **preprocessing, 'training_features': z,
                'dual_weights': dual, 'intercept': target_mean, 'gamma': gamma,
                'alpha': alpha, 'kernel_column_mean': column_mean, 'kernel_grand_mean': grand_mean}
    for count in NEIGHBOURS:
        for weighting in ('uniform', 'inverse_distance'):
            result[f'neighbours:{count}:{weighting}'] = {
                'kind': 'neighbours', **preprocessing, 'training_features': z,
                'training_targets': y, 'count': count, 'weighting': weighting}
    return result


def prediction(model, x):
    if model['kind'] == 'linear':
        return predict(model, x)
    clean = np.where(np.isfinite(x), x, model['medians'])
    z = (clean - model['mean']) / model['scale']
    squared = distance(z, model['training_features'])
    if model['kind'] == 'rbf':
        kernel = np.exp(-model['gamma'] * squared / z.shape[1])
        centered = (kernel - kernel.mean(axis=1, keepdims=True)
                    - model['kernel_column_mean'][None, :] + model['kernel_grand_mean'])
        values = model['intercept'] + centered @ model['dual_weights']
    else:
        count = min(model['count'], squared.shape[1])
        indices = np.argsort(squared, axis=1, kind='stable')[:, :count]
        if model['weighting'] == 'uniform':
            weights = np.ones_like(indices, dtype=float)
        else:
            weights = 1 / np.maximum(np.sqrt(np.take_along_axis(squared, indices, axis=1)), 1e-8)
        weights /= weights.sum(axis=1, keepdims=True)
        values = np.einsum('nk,nkj->nj', weights, model['training_targets'][indices])
    return np.clip(values, 0, 1)


def select(x, y, groups, vibe):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = {}
    for fold in range(3):
        train, valid = split != fold, split == fold
        for key, model in candidates(x[train], y[train]).items():
            values = prediction(model, x[valid])
            loss = score(y[valid], values)['acceptable_log_loss'] * sum(valid) if vibe else np.abs(y[valid] - values).sum()
            losses[key] = losses.get(key, 0.) + float(loss)
    chosen = {kind: min((key for key in losses if key.startswith(kind + ':')), key=losses.get)
              for kind in ('linear', 'rbf', 'neighbours')}
    chosen['selected_learner'] = min(losses, key=losses.get)
    return chosen


def load(dataset, external_datasets):
    if dataset == 'gold-vibes':
        snapshot = json.loads(SNAPSHOT.read_text())
        rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
        y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
        groups = [normalize(row['artist']) or row['video_id'] for row in rows]
        embedding = np.array([row['embedding_values'] for row in rows])
        qualities, names, exclusions, signatures, _ = external_head_features(external_datasets, rows)
        features = {'embedding': embedding, 'external_qualities': qualities,
                    'embedding_external_qualities': np.column_stack([embedding, qualities])}
        for source in external_datasets:
            columns = [i for i, name in enumerate(names) if name.startswith(source + ':')]
            features['expert:' + source] = qualities[:, columns]
        provenance = {'snapshot_sha256': digest(SNAPSHOT), 'external_datasets': external_datasets,
                      'external_identity_exclusions': exclusions, 'embedding_signatures': sorted(signatures),
                      'silver_rows_excluded': len(snapshot['silver'])}
        return rows, y, list(VIBES), groups, features, provenance
    path = OUT / f'{dataset}.features.json'
    rows = json.loads(path.read_text())['rows']
    names = rows[0]['target_names']
    selected = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
    selected = selected or list(range(len(names)))
    y = np.array([row['targets'] for row in rows], dtype=float)[:, selected]
    families, _, _, _ = feature_matrices(rows)
    features = {name: families[name] for name in ('attributes', 'embedding', 'attributes_embedding')}
    return rows, y, [names[i] for i in selected], [row['group'] for row in rows], features, {'source_sha256': digest(path)}


def serializable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    return value


def evaluate(dataset, external_datasets=()):
    with threadpool_limits(limits=1):
        return _evaluate(dataset, list(external_datasets))


def _evaluate(dataset, external_datasets):
    rows, y, names, groups, features, provenance = load(dataset, external_datasets)
    artifact_name = dataset if dataset != 'gold-vibes' else dataset + '-' + '-'.join(external_datasets)
    split = folds(groups)
    report = {'created_at': stamp(), 'dataset': dataset, 'rows': len(rows), 'target_names': names,
              'unique_groups': len(set(groups)), 'script_sha256': digest(__file__), **provenance,
              'validation': 'Same five artist/recording outer folds; three grouped inner folds select all preprocessing-dependent parameters and learner families.',
              'grid': {'linear_alphas': ALPHAS, 'rbf_gammas_per_dimension': GAMMAS,
                       'rbf_alphas': KERNEL_ALPHAS, 'neighbour_counts': NEIGHBOURS,
                       'neighbour_weights': ['uniform', 'inverse_distance']}, 'features': {},
              'limitations': ['Previously examined developer gold remains exploratory; no final winner is established by searching many methods.',
                              'Native task quality and six-vibe transfer remain separate.',
                              'Original corpus grouping and upstream-pretraining limitations still apply.']}
    predictions = {}
    for family, x in features.items():
        values = {name: np.zeros_like(y) for name in ('linear', 'rbf', 'neighbours', 'selected_learner')}
        choices = []
        for fold in range(5):
            train, valid = split != fold, split == fold
            chosen = select(x[train], y[train], np.array(groups)[train].tolist(), dataset == 'gold-vibes')
            fitted = candidates(x[train], y[train])
            for kind, key in chosen.items():
                values[kind][valid] = prediction(fitted[key], x[valid])
            choices.append({'fold': fold, 'chosen': chosen})
        report['features'][family] = {'methods': {name: score(y, p) if dataset == 'gold-vibes' else metrics(y, p, names)
                                                for name, p in values.items()}, 'choices_by_fold': choices}
        predictions[family] = {name: p.tolist() for name, p in values.items()}
        chosen = select(x, y, groups, dataset == 'gold-vibes')
        fitted = candidates(x, y)
        safe_family = family.replace(':', '-')
        for kind, key in chosen.items():
            save(OUT / 'models/learners' / f'{artifact_name}.{safe_family}.{kind}.json', {
                 'format': 'external-research-learner-1', 'dataset': dataset, 'family': family,
                 'target_names': names, 'training_ids': [row.get('id', row.get('video_id')) for row in rows],
                 'parameter_key': key, 'model': serializable(fitted[key]), 'provenance': provenance})
        save(OUT / f'{artifact_name}.learners.report.json', report)
        print(json.dumps({'dataset': dataset, 'family': family, 'learners': {
            key: {k: v for k, v in value.items() if k != 'targets'}
            for key, value in report['features'][family]['methods'].items()}}), flush=True)
    save(OUT / f'{artifact_name}.learners.oof.json', {'target_names': names, 'observed': y.tolist(),
         'rows': [{'id': row.get('id', row.get('video_id')), 'group': group, 'fold': int(fold)}
                  for row, group, fold in zip(rows, groups, split)], 'predictions': predictions})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset')
    parser.add_argument('--external-datasets', nargs='*', default=['soundtracks-set2', 'cal500', 'emotify', 'pmemo'])
    args = parser.parse_args()
    evaluate(args.dataset, args.external_datasets)


if __name__ == '__main__':
    main()
