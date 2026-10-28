"""Compare song-ranking suitability and every subset of native dataset experts."""
from itertools import combinations
import json

import numpy as np
from scipy.optimize import minimize
from threadpoolctl import threadpool_limits

from .external_binary_calibration import (balanced_log_loss, fit, metrics, predict,
                                          sample_weights, select)
from .external_experiment import OUT, digest, normalize, save, stamp
from .external_learners import serializable
from .external_training import SEED, folds
from .external_transfer import SNAPSHOT, VIBES, external_head_features


def weights(experts, y):
    n, m, k = experts.shape
    labels = (y > 0).astype(float)
    importance = sample_weights(labels)
    equality = np.zeros((k, m * k))
    for column in range(k):
        equality[column, column::k] = 1

    def objective(flat):
        w = flat.reshape(m, k)
        p = np.clip(np.einsum('nmk,mk->nk', experts, w), 1e-10, 1 - 1e-10)
        value = -(importance * (labels * np.log(p) + (1 - labels) * np.log1p(-p))).mean()
        value += .05 * (w * np.log(w * m)).sum() / k
        delta = importance * (p - labels) / (p * (1 - p) * n * k)
        gradient = np.einsum('nmk,nk->mk', experts, delta) + .05 * (np.log(w * m) + 1) / k
        return float(value), gradient.ravel()

    result = minimize(objective, np.full(m * k, 1 / m), jac=True, method='SLSQP',
                      bounds=[(1e-6, 1.)] * (m * k),
                      constraints=[{'type': 'eq', 'fun': lambda flat: equality @ flat - 1,
                                    'jac': lambda flat: equality}], options={'maxiter': 500, 'ftol': 1e-10})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('Per-vibe pooling failed: ' + result.message)
    fitted = result.x.reshape(m, k)
    return fitted / fitted.sum(0)


def candidate_predictions(experts, candidate):
    return np.einsum('nmk,mk->nk', experts[:, candidate['indices']], candidate['weights'])


def fit_combinations(experts, y, names):
    result = {}
    for size in range(1, len(names) + 1):
        for members in combinations(range(len(names)), size):
            subset = experts[:, members, :]
            for policy in ('expert',) if size == 1 else ('equal', 'weighted_per_vibe'):
                fitted = weights(subset, y) if policy == 'weighted_per_vibe' else np.full((size, len(VIBES)), 1 / size)
                values = np.einsum('nmk,mk->nk', subset, fitted)
                key = policy + ':' + '+'.join(names[i] for i in members)
                result[key] = {'indices': list(members), 'members': [names[i] for i in members],
                               'policy': policy, 'weights': fitted.tolist(),
                               'inner_balanced_binary_log_loss': balanced_log_loss(y, values)}
    return result


def choose(candidates, consensus_only=False):
    return min((key for key, candidate in candidates.items() if not consensus_only or len(candidate['indices']) > 1),
               key=lambda key: (candidates[key]['inner_balanced_binary_log_loss'], len(candidates[key]['indices']), key))


def crossfit(features, y, groups):
    split = folds(groups, count=3, seed=SEED + 1)
    values = np.zeros((len(y), len(features), len(VIBES)))
    for fold in range(3):
        train, valid = split != fold, split == fold
        for i, x in enumerate(features.values()):
            alpha = select(x[train], y[train], np.array(groups)[train].tolist())
            values[valid, i] = predict(fit(x[train], y[train], alpha), x[valid])
    return values


def evaluate(sources):
    with threadpool_limits(limits=1):
        return _evaluate(sources)


def _evaluate(sources):
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    y = np.array([[int(name in row['labels']) for name in VIBES] for row in rows], dtype=float)
    groups = [normalize(row['artist']) or row['video_id'] for row in rows]
    qualities, names, exclusions, signatures, heads = external_head_features(sources, rows)
    features = {'embedding': np.array([row['embedding_values'] for row in rows])}
    for source in sources:
        columns = [i for i, name in enumerate(names) if name.startswith(source + ':')]
        features[source] = qualities[:, columns]
    split = folds(groups)
    experts = np.zeros((len(rows), len(features), len(VIBES)))
    methods = {name: np.zeros_like(y) for name in ('selected_candidate', 'selected_consensus', 'equal_all', 'weighted_all')}
    choices = []
    for fold in range(5):
        train, valid = split != fold, split == fold
        inner = crossfit({name: x[train] for name, x in features.items()}, y[train], np.array(groups)[train].tolist())
        candidates = fit_combinations(inner, y[train], list(features))
        for i, x in enumerate(features.values()):
            alpha = select(x[train], y[train], np.array(groups)[train].tolist())
            experts[valid, i] = predict(fit(x[train], y[train], alpha), x[valid])
        for key, candidate in candidates.items():
            methods.setdefault(key, np.zeros_like(y))[valid] = candidate_predictions(experts[valid], candidate)
        selected, selected_consensus = choose(candidates), choose(candidates, True)
        methods['selected_candidate'][valid] = methods[selected][valid]
        methods['selected_consensus'][valid] = methods[selected_consensus][valid]
        methods['equal_all'][valid] = experts[valid].mean(1)
        methods['weighted_all'][valid] = np.einsum('nmk,mk->nk', experts[valid], weights(inner, y[train]))
        choices.append({'fold': fold, 'selected_candidate': selected, 'selected_consensus': selected_consensus,
                        'candidates': candidates})
        print(json.dumps({'vibe_ranking_outer_fold': fold, 'sources': sources, 'candidates': len(candidates)}), flush=True)
    results = {name: metrics(y, values) for name, values in methods.items()}
    slug = '+'.join(sources)
    report = {'created_at': stamp(), 'sources': sources, 'rows': len(rows), 'methods': results,
              'expert_names': list(features), 'nonempty_subsets': 2 ** len(features) - 1,
              'choices_by_fold': choices, 'script_sha256': digest(__file__), 'snapshot_sha256': digest(SNAPSHOT),
              'external_identity_exclusions': exclusions, 'embedding_signatures': sorted(signatures),
              'silver_rows_excluded': len(snapshot['silver']),
              'validation': 'Five artist-group outer folds; three inner meta-folds; calibration regularization selected inside each meta-training fold. Subset selection and per-vibe pooling use outer-training labels only.',
              'target_interpretation': 'Selected acceptable vibes versus unselected vibes, with each vibe scored independently. Original human annotations are unchanged.',
              'limitations': ['Unselected does not establish an explicit listener rejection; positives may be incomplete.',
                             'The small previously examined gold snapshot remains exploratory and supplies only calibration, not scalable native supervision.',
                             'Class-balanced scores rank songs within a vibe; they are not population-calibrated suitability probabilities.',
                             'Inner labels both fit pooling weights and choose subsets. Only outer scores estimate that complete procedure.',
                             'Best fixed subsets found after inspecting all results are descriptive, not independently validated.']}
    save(OUT / f'vibe-ranking-{slug}.report.json', report)
    save(OUT / f'vibe-ranking-{slug}.oof.json', {'ids': [row['video_id'] for row in rows], 'folds': split.tolist(),
         'observed': y.tolist(), 'predictions': {name: value.tolist() for name, value in methods.items()}})
    inner = crossfit(features, y, groups)
    combinations = fit_combinations(inner, y, list(features))
    calibrators = {}
    for name, x in features.items():
        calibrators[name] = serializable(fit(x, y, select(x, y, groups)))
    save(OUT / 'models' / f'vibe-ranking-{slug}.json', {'format': 'external-research-vibe-ranking-1',
         'vibes': VIBES, 'expert_names': list(features), 'native_heads': heads, 'calibrators': calibrators,
         'combinations': combinations, 'selected_candidate': choose(combinations),
         'selected_consensus': choose(combinations, True), 'gold_training_ids': [row['video_id'] for row in rows],
         'report_sha256': digest(OUT / f'vibe-ranking-{slug}.report.json')})
    return report
