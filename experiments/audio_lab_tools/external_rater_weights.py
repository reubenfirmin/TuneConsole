"""Test whether weighting native judgments by listener count improves Emotify."""
import json

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_training import (ALPHAS, SEED, bootstrap_improvement, folds, metrics,
                                predict, standardize)
from .external_learners import load, serializable
from .external_transfer import SNAPSHOT, VIBES, external_head_features

POWERS = (0., .5, 1.)


def fit_path(x, y, counts, power):
    z, medians, mean, scale = standardize(x)
    importance = np.asarray(counts, dtype=float) ** power
    importance /= importance.mean()
    center = np.average(z, axis=0, weights=importance)
    intercept = np.average(y, axis=0, weights=importance)
    root = np.sqrt(importance)
    u, singular, vt = np.linalg.svd((z - center) * root[:, None], full_matrices=False)
    projected = u.T @ ((y - intercept) * root[:, None])
    return [{'medians': medians, 'mean': mean + scale * center, 'scale': scale,
             'weights': (vt.T * (singular / (singular * singular + alpha))) @ projected,
             'intercept': intercept, 'alpha': alpha, 'rater_count_power': power}
            for alpha in ALPHAS]


def select(x, y, groups, counts):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = {(power, alpha): 0. for power in POWERS for alpha in ALPHAS}
    for fold in range(3):
        train, valid = split != fold, split == fold
        for power in POWERS:
            for model in fit_path(x[train], y[train], counts[train], power):
                losses[(power, model['alpha'])] += float(np.abs(y[valid] - predict(model, x[valid])).sum())
    result = {str(power): min((key for key in losses if key[0] == power), key=losses.get)
              for power in POWERS}
    result['selected_power'] = min(losses, key=losses.get)
    return result


def evaluate(dataset='emotify'):
    from .external_binary_calibration import fit as binary_fit, predict as binary_predict, metrics as ranking_metrics, select as binary_select
    with threadpool_limits(limits=1):
        rows, y, names, groups, features, provenance = load(dataset, ())
        x = features['embedding']
        counts = np.array([row['rater_count'] for row in rows], dtype=float)
        if (counts < 1).any() or not np.isfinite(counts).all():
            raise ValueError('Listener counts must be explicit positive observations')
        split = folds(groups)
        saved = {name: np.zeros_like(y, dtype=float) for name in [*map(str, POWERS), 'selected_power']}
        choices = []
        for fold in range(5):
            train, valid = split != fold, split == fold
            chosen = select(x[train], y[train], np.array(groups)[train].tolist(), counts[train])
            for name, (power, alpha) in chosen.items():
                model = next(value for value in fit_path(x[train], y[train], counts[train], power)
                             if value['alpha'] == alpha)
                saved[name][valid] = predict(model, x[valid])
            choices.append({'fold': fold, 'choices': chosen})
        native = {name: {**metrics(y, values, names),
                        'versus_unweighted': bootstrap_improvement(y, saved['0.0'], values, groups, list(range(len(names))))}
                  for name, values in saved.items()}
        chosen = select(x, y, groups, counts)
        snapshot = json.loads(SNAPSHOT.read_text())
        gold = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
        _, _, exclusions, signatures, _ = external_head_features([dataset], gold)
        if exclusions[dataset]:
            raise ValueError('This comparison requires explicitly refitting around newly identified gold overlaps')
        gold_x = np.array([row['embedding_values'] for row in gold])
        gold_y = np.array([[int(vibe in row['labels']) for vibe in VIBES] for row in gold], dtype=float)
        gold_groups = [normalize(row['artist']) or row['video_id'] for row in gold]
        gold_split = folds(gold_groups)
        ranked, ranking_results = {}, {}
        for name, (power, alpha) in chosen.items():
            head = next(value for value in fit_path(x, y, counts, power) if value['alpha'] == alpha)
            qualities = predict(head, gold_x)
            values = np.zeros_like(gold_y)
            for fold in range(5):
                train, valid = gold_split != fold, gold_split == fold
                strength = binary_select(qualities[train], gold_y[train], np.array(gold_groups)[train].tolist())
                values[valid] = binary_predict(binary_fit(qualities[train], gold_y[train], strength), qualities[valid])
            ranked[name] = values.tolist()
            ranking_results[name] = ranking_metrics(gold_y, values)
            strength = binary_select(qualities, gold_y, gold_groups)
            save(OUT / 'models' / f'{dataset}.rater-weight-{name}.json', {
                 'format': 'external-research-rater-weighted-vibe-ranking-1', 'target_names': names,
                 'vibes': VIBES, 'head': serializable(head), 'source_provenance': provenance,
                 'calibrator': serializable(binary_fit(qualities, gold_y, strength)),
                 'embedding_signatures': sorted(signatures), 'snapshot_sha256': digest(SNAPSHOT)})
        report = {'created_at': stamp(), 'dataset': dataset, 'rows': len(rows),
                  'native_methods': native, 'ranking_methods': ranking_results,
                  'choices_by_fold': choices, 'full_training_choices': chosen,
                  'source_provenance': provenance, 'script_sha256': digest(__file__),
                  'listener_count_range': [float(counts.min()), float(counts.max())],
                  'validation': 'Native five/three grouped folds select ridge alpha and listener-count power. Zero weighting is included. Full native head parameters depend only on native validation; five/three artist-group gold folds fit independent binary ranking calibration.',
                  'limitations': ['More ratings do not establish less biased or more representative judgments.',
                                 'This weights listener counts, not estimated latent truth or individual listener competence.',
                                 'Emotify lacks artist identities; native validation is by recording.',
                                 'Gold is small and previously examined; unselected alternatives are not explicit rejections.',
                                 'Balanced ranking outputs are not population probabilities.']}
        save(OUT / f'{dataset}.rater-weights.report.json', report)
        save(OUT / f'{dataset}.rater-weights.oof.json', {'native_ids': [row['id'] for row in rows],
             'native_observed': y.tolist(), 'native_predictions': {name: value.tolist() for name, value in saved.items()},
             'gold_ids': [row['video_id'] for row in gold], 'gold_observed': gold_y.tolist(),
             'gold_predictions': ranked, 'gold_folds': gold_split.tolist()})
        print(json.dumps({'rater_weighting': dataset, 'native_mae': {name: value['mae'] for name, value in native.items()},
                          'ranking_ap': {name: value['macro_average_precision'] for name, value in ranking_results.items()}}), flush=True)
        return report
