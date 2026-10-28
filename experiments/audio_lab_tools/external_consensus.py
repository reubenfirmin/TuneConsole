"""Research-only consensus of independently calibrated external dataset experts.

No prediction becomes a human label. Gold is a small calibration/evaluation
snapshot; external native labels provide each expert's underlying supervision.
"""
import argparse
from itertools import combinations
import json
import time

import numpy as np
from scipy.optimize import minimize
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_training import SEED, fit_path, folds, predict
from .external_transfer import (SNAPSHOT, VIBES, external_head_features,
                                probabilities, score, select_vibe_alpha)

ENTROPY_PENALTY = .05
SEARCH_VERSION = 2


def fit_expert(x, y, groups, kind='linear'):
    if kind == 'softmax':
        from .external_calibration import fit_selected
        return fit_selected(x, y, groups)
    if kind != 'linear':
        from .external_learners import candidates, select
        key = select(x, y, groups, True)[kind]
        return {**candidates(x, y)[key], 'parameter_key': key}
    alpha = select_vibe_alpha(x, y, groups)
    return fit_path(x, y, [alpha])[0]


def expert_prediction(model, x):
    if model.get('kind') == 'softmax':
        from .external_calibration import probabilities as softmax_probabilities
        return softmax_probabilities(model, x)
    if model.get('kind'):
        from .external_learners import prediction
        return probabilities(prediction(model, x))
    return probabilities(predict(model, x))


def crossfit_experts(features, y, groups, kind='linear'):
    """Each meta-training prediction excludes that row and its artist group."""
    split = folds(groups, count=3, seed=SEED + 1)
    predictions = np.zeros((len(y), len(features), len(VIBES)))
    for fold in range(3):
        train, valid = split != fold, split == fold
        for i, (name, x) in enumerate(features.items()):
            expert_kind = kind.get(name, 'linear') if isinstance(kind, dict) else kind
            fitted = fit_expert(x[train], y[train], np.array(groups)[train].tolist(), expert_kind)
            predictions[valid, i] = expert_prediction(fitted, x[valid])
    return predictions


def mixture_weights(experts, y):
    """Convex log-loss pooling, with a fixed weak prior toward equal weights."""
    count = experts.shape[1]
    acceptable = np.einsum('nmk,nk->nm', experts, y > 0)
    uniform = np.full(count, 1 / count)

    def objective(weights):
        mass = np.maximum(acceptable @ weights, 1e-12)
        entropy = np.sum(weights * np.log(weights * count))
        value = -np.log(mass).mean() + ENTROPY_PENALTY * entropy
        gradient = -(acceptable / mass[:, None]).mean(axis=0)
        gradient += ENTROPY_PENALTY * (np.log(weights * count) + 1)
        return value, gradient

    result = minimize(objective, uniform, jac=True, method='SLSQP',
                      bounds=[(1e-6, 1.)] * count,
                      constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1,
                                    'jac': lambda w: np.ones_like(w)}],
                      options={'maxiter': 300, 'ftol': 1e-10})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('Consensus weight optimization failed: ' + result.message)
    return result.x / result.x.sum()


def disagreement(experts):
    mean = experts.mean(axis=1)
    return np.mean(np.sum(experts * np.log(np.maximum(experts, 1e-12) /
                                          np.maximum(mean[:, None], 1e-12)), axis=2), axis=1)


def fit_combinations(experts, y, names):
    """Search every subset using only outer-training cross-fitted predictions.

    The same inner labels fit weights and rank candidates. Only the untouched
    outer fold estimates the performance of that complete selection procedure.
    """
    candidates = {}
    for size in range(1, len(names) + 1):
        for members in combinations(range(len(names)), size):
            subset = experts[:, members, :]
            policies = ['expert'] if size == 1 else ['equal', 'weighted']
            for policy in policies:
                weights = mixture_weights(subset, y) if policy == 'weighted' else np.full(size, 1 / size)
                values = np.einsum('nmk,m->nk', subset, weights)
                name = policy + ':' + '+'.join(names[i] for i in members)
                candidates[name] = {'members': [names[i] for i in members], 'indices': list(members),
                                    'policy': policy, 'weights': weights.tolist(),
                                    'inner_acceptable_log_loss': score(y, values)['acceptable_log_loss']}
    return candidates


def select_combination(candidates, consensus_only=False):
    eligible = [name for name, value in candidates.items()
                if not consensus_only or len(value['members']) > 1]
    return min(eligible, key=lambda name: (candidates[name]['inner_acceptable_log_loss'],
                                         len(candidates[name]['members']), name))


def pool(experts, candidate):
    return np.einsum('nmk,m->nk', experts[:, candidate['indices'], :], candidate['weights'])


def evaluate(datasets, native_learner='ridge', calibrator='linear'):
    with threadpool_limits(limits=1):
        return _evaluate(datasets, native_learner, calibrator)


def _evaluate(datasets, native_learner='ridge', calibrator='linear'):
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
    groups = [normalize(row['artist']) or row['video_id'] for row in rows]
    if native_learner == 'ridge':
        qualities, names, exclusions, signatures, native_heads = external_head_features(datasets, rows)
    else:
        from .external_nonlinear import head_features
        qualities, names, exclusions, signatures, native_heads = head_features(datasets, rows, native_learner)
    features = {'embedding': np.array([row['embedding_values'] for row in rows])}
    feature_names = {'embedding': [f'musicnn_{i:03d}' for i in range(200)]}
    for dataset in datasets:
        columns = [i for i, name in enumerate(names) if name.startswith(dataset + ':')]
        features[dataset] = qualities[:, columns]
        feature_names[dataset] = [names[i] for i in columns]
    outer = folds(groups)
    experts = np.zeros((len(rows), len(features), len(VIBES)))
    methods = {name: np.zeros_like(y) for name in
               ['equal_external', 'equal_all', 'weighted_external', 'weighted_all',
                'selected_combination', 'selected_candidate']}
    weights_by_fold, alphas, combination_folds = [], [], []
    for fold in range(5):
        train, test = outer != fold, outer == fold
        training_groups = np.array(groups)[train].tolist()
        inner = crossfit_experts({name: x[train] for name, x in features.items()}, y[train], training_groups, calibrator)
        all_weights = mixture_weights(inner, y[train])
        external_weights = mixture_weights(inner[:, 1:], y[train])
        combinations_by_name = fit_combinations(inner, y[train], list(features))
        chosen = {}
        for i, (name, x) in enumerate(features.items()):
            expert_kind = calibrator.get(name, 'linear') if isinstance(calibrator, dict) else calibrator
            fitted = fit_expert(x[train], y[train], training_groups, expert_kind)
            experts[test, i] = expert_prediction(fitted, x[test])
            chosen[name] = fitted.get('parameter_key', fitted.get('alpha'))
        methods['equal_external'][test] = experts[test, 1:].mean(axis=1)
        methods['equal_all'][test] = experts[test].mean(axis=1)
        methods['weighted_external'][test] = np.einsum('nmk,m->nk', experts[test, 1:], external_weights)
        methods['weighted_all'][test] = np.einsum('nmk,m->nk', experts[test], all_weights)
        for name, candidate in combinations_by_name.items():
            if name not in methods:
                methods[name] = np.zeros_like(y)
            methods[name][test] = pool(experts[test], candidate)
        selected = select_combination(combinations_by_name)
        selected_consensus = select_combination(combinations_by_name, consensus_only=True)
        methods['selected_candidate'][test] = methods[selected][test]
        methods['selected_combination'][test] = methods[selected_consensus][test]
        combination_folds.append({'fold': fold, 'selected_candidate': selected,
                                  'selected_combination': selected_consensus,
                                  'candidates': combinations_by_name})
        weights_by_fold.append({'fold': fold, 'all': dict(zip(features, all_weights.tolist())),
                                'external': dict(zip(datasets, external_weights.tolist()))})
        alphas.append(chosen)
    for i, name in enumerate(features):
        methods['expert:' + name] = experts[:, i]
    results = {name: score(y, values) for name, values in methods.items()}
    unseen = np.array([row['video_id'] not in snapshot['teacher']['training_video_ids'] for row in rows])
    js = disagreement(experts[:, 1:])
    diagnostics = []
    for threshold in [.01, .03, .1]:
        subset = js <= threshold
        diagnostics.append({'maximum_js_disagreement': threshold, 'rows': int(subset.sum()),
                            'fraction': float(subset.mean()),
                            'weighted_all': score(y[subset], methods['weighted_all'][subset]) if subset.any() else None})
    pairwise = {}
    failures = {name: ~(y[np.arange(len(y)), values.argmax(axis=1)] > 0)
                for name, values in methods.items() if name.startswith('expert:')}
    for left, a in failures.items():
        pairwise[left] = {right: float(np.corrcoef(a, b)[0, 1]) if a.std() and b.std() else None
                          for right, b in failures.items()}
    slug = '+'.join(datasets)
    if native_learner != 'ridge' or calibrator != 'linear':
        calibration_slug = ('mixed-' + '+'.join(name + '-' + kind for name, kind in sorted(calibrator.items()))
                            if isinstance(calibrator, dict) else calibrator)
        slug += f'__native-{native_learner}__calibrator-{calibration_slug}'
    report = {'created_at': stamp(), 'search_version': SEARCH_VERSION, 'datasets': datasets, 'gold_rows': len(rows),
              'silver_rows_excluded': len(snapshot['silver']), 'vibes': VIBES,
              'native_learner': native_learner, 'calibrator_learner': calibrator,
              'snapshot_sha256': digest(SNAPSHOT), 'script_sha256': digest(__file__),
              'embedding_signatures': sorted(signatures), 'external_identity_exclusions': exclusions,
              'methods': results, 'weights_by_fold': weights_by_fold, 'expert_alphas_by_fold': alphas,
              'combination_search': {'expert_names': list(features),
                                     'subsets': 2 ** len(features) - 1,
                                     'consensus_subsets': 2 ** len(features) - len(features) - 1,
                                     'policies': ['equal', 'weighted'],
                                     'selection_objective': 'Acceptable-label log loss on outer-training cross-fitted expert predictions; weights fit on those same inner labels.',
                                     'selected_combination': 'At least two experts; subset and pooling policy selected separately within each outer training fold.',
                                     'selected_candidate': 'Same search, also allowing individual experts.',
                                     'folds': combination_folds},
              'teacher_unseen_subset': {name: score(y[unseen], values[unseen]) for name, values in methods.items()},
              'disagreement_checks': diagnostics, 'expert_error_correlations': pairwise,
              'validation': 'Five artist-group outer folds; three meta-training folds; expert alpha selected inside each meta-training fold.',
              'weight_policy': {'objective': 'acceptable-label log loss plus entropy penalty toward uniform weights',
                                'entropy_penalty': ENTROPY_PENALTY, 'chosen_before_evaluation': True},
              'limitations': ['Only 55 previously examined developer songs; this is exploratory calibration and evaluation.',
                              'External native labels are not interchangeable with vibe labels; each expert has its own six-vibe calibrator.',
                              'Experts share MusiCNN embeddings and may make correlated mistakes; agreement is not established confidence.',
                              'The best fixed combination after inspecting outer results is descriptive; use selected_combination or selected_candidate to assess the search procedure.',
                              'Unknown upstream pretraining overlap, artist aliases, and unavailable Emotify artist identities remain.',
                              'No pseudo-label supervision, database writes, vibe changes, or model activation.']}
    save(OUT / f'consensus-{slug}.report.json', report)
    save(OUT / f'consensus-{slug}.oof.json', {'rows': [{'video_id': row['video_id'], 'fold': int(fold)}
         for row, fold in zip(rows, outer)], 'observed': y.tolist(), 'expert_names': list(features),
         'expert_predictions': experts.tolist(), 'predictions': {name: value.tolist() for name, value in methods.items()},
         'external_js_disagreement': js.tolist()})
    full_inner = crossfit_experts(features, y, groups, calibrator)
    calibrators = {}
    for name, x in features.items():
        expert_kind = calibrator.get(name, 'linear') if isinstance(calibrator, dict) else calibrator
        fitted = fit_expert(x, y, groups, expert_kind)
        calibrators[name] = {'feature_names': feature_names[name], **{
            key: value.tolist() if isinstance(value, np.ndarray) else value for key, value in fitted.items()}}
    full_combinations = fit_combinations(full_inner, y, list(features))
    save(OUT / 'models' / f'consensus-{slug}.json', {'format': 'external-research-consensus-2',
         'vibes': VIBES, 'expert_names': list(features), 'native_heads': native_heads, 'calibrators': calibrators,
         'weights_all': mixture_weights(full_inner, y).tolist(),
         'weights_external': mixture_weights(full_inner[:, 1:], y).tolist(),
         'combinations': full_combinations,
         'selected_combination': select_combination(full_combinations, consensus_only=True),
         'selected_candidate': select_combination(full_combinations),
         'gold_training_ids': [row['video_id'] for row in rows], 'report_sha256': digest(OUT / f'consensus-{slug}.report.json')})
    lines = ['# Dataset-expert consensus', '', f"Datasets: {', '.join(datasets)}. {len(rows)} developer songs.", '',
             '| Method | Top choice acceptable | Acceptable-label log loss |', '| --- | ---: | ---: |']
    for name, value in results.items():
        lines.append(f"| {name} | {value['top_1_acceptable']:.1%} | {value['acceptable_log_loss']:.4f} |")
    lines.extend(['', f"Search: {2 ** len(features) - 1} nonempty expert subsets. Every multi-expert subset has equal and learned-weight pooling.",
                  'The selected rows choose subset and policy inside each outer training fold. Fixed combinations are exploratory comparisons, not an independently validated winner.'])
    lines.extend(['', *['- ' + value for value in report['limitations']], ''])
    (OUT / f'consensus-{slug}.report.md').write_text('\n'.join(lines))
    print(json.dumps({'consensus_datasets': datasets, 'methods': results}), flush=True)
    return report


def watch():
    previous = None
    while True:
        suite = json.loads((OUT / 'suite-status.json').read_text())
        completed = [row['dataset'] for row in suite['datasets']
                     if row['state'] == 'complete' and row['dataset'] != 'soundtracks-all']
        if len(completed) >= 2 and tuple(completed) != previous:
            evaluate(completed)
            previous = tuple(completed)
            save(OUT / 'consensus-status.json', {'updated_at': stamp(), 'datasets': completed,
                 'state': 'complete' if suite['state'].startswith('complete') else 'watching'})
        if suite['state'].startswith('complete'):
            return
        time.sleep(20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--native-learner', choices=['ridge', 'rbf', 'selected_learner'], default='ridge')
    parser.add_argument('--calibrator', choices=['linear', 'rbf', 'neighbours', 'selected_learner', 'softmax'], default='linear')
    parser.add_argument('datasets', nargs='*')
    args = parser.parse_args()
    if args.watch:
        watch()
    else:
        evaluate(args.datasets or ['soundtracks-set2', 'cal500', 'emotify'], args.native_learner, args.calibrator)


if __name__ == '__main__':
    main()
