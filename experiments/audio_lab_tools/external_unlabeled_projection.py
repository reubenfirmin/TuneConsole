"""Inductive PCA/whitening from unlabeled external audio, without pseudo-labels."""
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_training import ALPHAS, SEED, folds, metrics, predict, standardize
from .external_transfer import SNAPSHOT, VIBES, score
from .external_secondary import artist_keys, identity

DIMENSIONS = (8, 16, 32, 64, 128, 200)
WHITENING = (0., .5, 1.)


def target(dataset):
    if dataset == 'gold-vibes':
        snapshot = json.loads(SNAPSHOT.read_text())
        rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
        y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
        return rows, np.array([row['embedding_values'] for row in rows]), y, list(VIBES), [normalize(row['artist']) or row['video_id'] for row in rows]
    rows = json.loads((OUT / f'{dataset}.features.json').read_text())['rows']
    names = rows[0]['target_names']
    selected = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
    selected = selected or list(range(len(names)))
    x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values'] for row in rows])
    y = np.array([row['targets'] for row in rows], dtype=float)[:, selected]
    return rows, x, y, [names[i] for i in selected], [row['group'] for row in rows]


def projection_pool(dataset, targets, sources):
    artists = {key for row in targets for key in artist_keys(row.get('artist', '').split('+')[0])}
    titles = {identity(row['title']) for row in targets if row.get('title')}
    hashes = {row['audio_sha256'] for row in targets if row.get('audio_sha256')}
    eligible, excluded = {}, []
    source_hashes = {}
    for source in sources:
        if (source.removesuffix('-dynamic') == dataset.removesuffix('-dynamic') or
                (source.startswith('soundtracks') and dataset.startswith('soundtracks')) or
                (source.startswith('jamendo-human') and dataset.startswith('jamendo-human'))):
            continue
        path = OUT / f'{source}.features.json'
        source_hashes[source] = digest(path)
        for row in json.loads(path.read_text())['rows']:
            artist = artist_keys(row.get('artist', '').split('+')[0])
            title = identity(row.get('title', ''))
            if artist & artists or (title and title in titles) or row['audio_sha256'] in hashes:
                excluded.append(row['id'])
                continue
            eligible.setdefault(row['audio_sha256'], {key: row[key] for key in ('id', 'feature_file', 'audio_sha256')})
    rows = list(eligible.values())
    if len(rows) < 200:
        raise ValueError('Need at least 200 disjoint unlabeled audio recordings')
    x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values'] for row in rows])
    return x, {'recordings': rows, 'excluded_ids': excluded, 'source_features_sha256': source_hashes,
               'policy': 'Audio identity, artist/title strings, and embeddings only; native target labels and prediction scores are unused. Target corpus is completely excluded.'}


def fit_projection(x):
    z, medians, mean, scale = standardize(x)
    _, singular, components = np.linalg.svd(z, full_matrices=False)
    deviation = np.maximum(singular / np.sqrt(len(x)), 1e-6)
    return {'medians': medians, 'mean': mean, 'scale': scale, 'components': components.T,
            'component_deviation': deviation}


def transform(model, x, dimensions, whitening):
    z = (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']
    return (z @ model['components'][:, :dimensions]) / model['component_deviation'][:dimensions] ** whitening


def ridge_path(x, y):
    # Pool-defined component scaling is deliberately preserved; standardizing
    # again on the target would cancel the whitening experiment exactly.
    mean, intercept = x.mean(0), y.mean(0)
    u, s, vt = np.linalg.svd(x - mean, full_matrices=False)
    projected = u.T @ (y - intercept)
    return [{'medians': np.zeros(x.shape[1]), 'mean': mean, 'scale': np.ones(x.shape[1]),
             'weights': (vt.T * (s / (s * s + alpha))) @ projected, 'intercept': intercept,
             'alpha': alpha} for alpha in ALPHAS]


def choose(features, y, groups, vibe):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = {}
    for fold in range(3):
        train, valid = split != fold, split == fold
        for key, x in features.items():
            for model in ridge_path(x[train], y[train]):
                values = predict(model, x[valid])
                loss = score(y[valid], values)['acceptable_log_loss'] * sum(valid) if vibe else np.abs(y[valid] - values).sum()
                candidate = (*key, model['alpha'])
                losses[candidate] = losses.get(candidate, 0.) + float(loss)
    return min(losses, key=losses.get)


def evaluate_ranking(features, y, groups, rows, projection, provenance):
    from .external_binary_calibration import (fit, predict as binary_predict,
                                               balanced_log_loss, metrics as ranking_metrics)
    from .external_learners import serializable

    def choose_binary(training_features, labels, training_groups):
        split = folds(training_groups, count=3, seed=SEED + 1)
        losses = {}
        for fold in range(3):
            train, valid = split != fold, split == fold
            for key, x in training_features.items():
                for alpha in ALPHAS:
                    fitted = fit(x[train], labels[train], alpha, standardize_features=False)
                    loss = balanced_log_loss(labels[valid], binary_predict(fitted, x[valid])) * sum(valid)
                    losses[(*key, alpha)] = losses.get((*key, alpha), 0.) + loss
        return min(losses, key=losses.get)

    split = folds(groups)
    values = np.zeros_like(y, dtype=float)
    choices = []
    for fold in range(5):
        train, valid = split != fold, split == fold
        dimensions, whitening, alpha = choose_binary({key: x[train] for key, x in features.items()},
                                                     y[train], np.array(groups)[train].tolist())
        x = features[(dimensions, whitening)]
        model = fit(x[train], y[train], alpha, standardize_features=False)
        values[valid] = binary_predict(model, x[valid])
        choices.append({'fold': fold, 'dimensions': dimensions, 'whitening': whitening, 'alpha': alpha})
    report = {'created_at': stamp(), 'metrics': ranking_metrics(y, values), 'choices_by_fold': choices,
              'pool_provenance': provenance, 'script_sha256': digest(__file__),
              'validation': 'Disjoint unlabeled projection pool; five artist-group gold outer folds; three inner folds select dimensions, whitening and binary ridge penalty by balanced log loss. Preserve pool-defined scaling rather than standardizing it away.',
              'limitations': ['Small previously examined developer snapshot; selected alternatives may be incomplete.',
                             'Class-balanced suitability scores are not population probabilities.']}
    save(OUT / 'gold-vibes.unlabeled-projection-ranking.report.json', report)
    save(OUT / 'gold-vibes.unlabeled-projection-ranking.oof.json', {
         'ids': [row['video_id'] for row in rows], 'folds': split.tolist(),
         'observed': (y > 0).astype(int).tolist(), 'predictions': values.tolist()})
    dimensions, whitening, alpha = choose_binary(features, y, groups)
    model = fit(features[(dimensions, whitening)], y, alpha, standardize_features=False)
    save(OUT / 'models/gold-vibes.unlabeled-projection-ranking.json', {
         'format': 'external-research-unlabeled-projection-ranking-1', 'vibes': VIBES,
         'projection': serializable(projection), 'dimensions': dimensions, 'whitening': whitening,
         'calibrator': serializable(model), 'pool_provenance': provenance,
         'training_ids': [row['video_id'] for row in rows]})
    print(json.dumps({'unlabeled_projection_ranking': report['metrics']}), flush=True)


def evaluate(dataset, sources):
    from .external_learners import serializable
    with threadpool_limits(limits=1):
        rows, x, y, names, groups = target(dataset)
        pool, provenance = projection_pool(dataset, rows, sources)
        projection = fit_projection(pool)
        features = {(dimensions, whitening): transform(projection, x, dimensions, whitening)
                    for dimensions in DIMENSIONS for whitening in WHITENING}
        split = folds(groups)
        values = np.zeros_like(y)
        choices = []
        for fold in range(5):
            train, valid = split != fold, split == fold
            choice = choose({key: value[train] for key, value in features.items()}, y[train],
                            np.array(groups)[train].tolist(), dataset == 'gold-vibes')
            dimensions, whitening, alpha = choice
            fitted = next(model for model in ridge_path(features[(dimensions, whitening)][train], y[train]) if model['alpha'] == alpha)
            values[valid] = predict(fitted, features[(dimensions, whitening)][valid])
            choices.append({'fold': fold, 'dimensions': dimensions, 'whitening': whitening, 'alpha': alpha})
        report = {'created_at': stamp(), 'dataset': dataset, 'rows': len(rows), 'unlabeled_recordings': len(pool),
                  'metrics': score(y, values) if dataset == 'gold-vibes' else metrics(y, values, names),
                  'choices_by_fold': choices, 'script_sha256': digest(__file__),
                  'validation': 'Five grouped outer folds; three inner folds choose PCA dimension, whitening and ridge alpha. Projection is fitted only on other corpora, with known target artist/title/audio overlaps removed.',
                  'grid': {'dimensions': DIMENSIONS, 'whitening': WHITENING, 'alphas': ALPHAS},
                  'limitations': ['Missing artist identities and aliases prevent complete cross-corpus identity exclusion.',
                                 'An unsupervised projection can discard emotionally useful low-variance components; held-out task performance decides usefulness.',
                                 'The gold snapshot remains small and previously examined.']}
        save(OUT / f'{dataset}.unlabeled-projection.report.json', report)
        save(OUT / f'{dataset}.unlabeled-projection.oof.json', {'ids': [row.get('id', row.get('video_id')) for row in rows],
             'folds': split.tolist(), 'observed': y.tolist(), 'predictions': values.tolist(), 'pool_provenance': provenance})
        dimensions, whitening, alpha = choose(features, y, groups, dataset == 'gold-vibes')
        fitted = next(model for model in ridge_path(features[(dimensions, whitening)], y) if model['alpha'] == alpha)
        save(OUT / 'models' / f'{dataset}.unlabeled-projection.json', {
             'format': 'external-research-unlabeled-projection-1', 'target_names': names,
             'projection': serializable(projection), 'dimensions': dimensions, 'whitening': whitening,
             'model': serializable(fitted), 'pool_provenance': provenance,
             'training_ids': [row.get('id', row.get('video_id')) for row in rows]})
        print(json.dumps({'unlabeled_projection': dataset, 'pool': len(pool), 'metrics': report['metrics']}), flush=True)
        if dataset == 'gold-vibes':
            evaluate_ranking(features, y, groups, rows, projection, provenance)
        return report
