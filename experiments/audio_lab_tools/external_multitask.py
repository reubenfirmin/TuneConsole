"""A shared audio representation with separate native human-label tasks."""
import itertools
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_learners import serializable
from .external_secondary import artist_keys, identity
from .external_training import (SEED, choose_alpha, fit_path, folds, metrics, predict,
                                standardize)
from .external_transfer import SNAPSHOT, VIBES, score, select_vibe_alpha

STEPS = 400
GRID = [(hidden, penalty, strength) for hidden, penalty, strength in
        itertools.product((32, 64), (.0001, .001), (0., .1))]


def load(sources):
    gold = json.loads(SNAPSHOT.read_text())['gold']
    artists = {key for row in gold for key in artist_keys(row.get('artist', '').split('+')[0])}
    titles = {identity(row['title']) for row in gold if row.get('title')}
    pair_data = json.loads((OUT / 'unlabeled-consistency/pairs.json').read_text())['pairs']
    pair_split = json.loads((OUT / 'unlabeled-consistency/pair-split.json').read_text())
    heldout_ids = set(pair_split['holdout_ids'])
    heldout_hashes = {pair['audio_sha256'] for pair in pair_data if pair['id'] in heldout_ids}
    unlabeled_hashes = {pair['audio_sha256'] for pair in pair_data if pair['id'] not in heldout_ids}
    if heldout_hashes & unlabeled_hashes:
        raise ValueError('Unlabeled training and robustness recordings overlap')
    pmemo = {row['id']: row for row in json.loads((OUT / 'pmemo.features.json').read_text())['rows']}

    def song_keys(row):
        title = identity(row.get('title', ''))
        return {(artist, title) for artist in artist_keys(row.get('artist', '').split('+')[0]) if title}

    heldout_keys, unlabeled_keys = set(), set()
    for pair in pair_data:
        keys = song_keys(pmemo[pair['id']])
        (heldout_keys if pair['id'] in heldout_ids else unlabeled_keys).update(keys)
    rows, targets, names, columns, exclusions, source_hashes = [], [], {}, {}, {}, {}
    robustness_exclusions = {}
    for source in sources:
        path = OUT / f'{source}.features.json'
        source_hashes[source] = digest(path)
        values = json.loads(path.read_text())['rows']
        excluded = [row['id'] for row in values if artist_keys(row.get('artist', '').split('+')[0]) & artists
                    or identity(row.get('title', '')) in titles]
        exclusions[source] = excluded
        values = [row for row in values if row['id'] not in set(excluded)]
        reserved = [row['id'] for row in values if row['audio_sha256'] in heldout_hashes
                    or song_keys(row) & heldout_keys]
        robustness_exclusions[source] = reserved
        values = [row for row in values if row['id'] not in set(reserved)]
        target_names = values[0]['target_names']
        selected = [i for i, name in enumerate(target_names) if name.startswith(('Emotion-', 'Usage-'))]
        selected = selected or list(range(len(target_names)))
        names[source] = [target_names[i] for i in selected]
        columns[source] = len(names[source])
        for row in values:
            withheld = row['audio_sha256'] in unlabeled_hashes or bool(song_keys(row) & unlabeled_keys)
            rows.append({**row, 'task': source, 'native_label_withheld': withheld})
            targets.append(np.asarray(row['targets'])[selected])
    offsets, position = {}, 0
    for source, size in columns.items():
        offsets[source] = (position, position + size)
        position += size
    y = np.zeros((len(rows), position))
    for i, (row, target) in enumerate(zip(rows, targets)):
        start, end = offsets[row['task']]
        y[i, start:end] = target
    x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values'] for row in rows])
    if x.shape[1] != 200 or not np.isfinite(x).all():
        raise ValueError('Shared model needs compatible finite audio embeddings')
    return rows, x, y, offsets, names, {'source_features_sha256': source_hashes,
         'gold_identity_exclusions': exclusions, 'robustness_identity_exclusions': robustness_exclusions,
         'native_labels_withheld_ids': [row['id'] for row in rows if row['native_label_withheld']],
         'unlabeled_recordings': len(unlabeled_hashes), 'robustness_recordings': len(heldout_hashes),
         'unlabeled_policy': 'Withhold native labels for the 127 unique training recordings and every known identity-matched excerpt across tasks. Keep these audio inputs in their global artist/audio fold. Remove all 32 robustness recordings and known identity matches entirely. Withheld native targets do not enter fitting, parameter selection, or the primary native result.'}


def connected_groups(rows):
    parents = list(range(len(rows)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    known = {}
    for i, row in enumerate(rows):
        keys = [('source_group', row['task'], row['group']), ('audio', row['audio_sha256'])]
        keys += [('artist', name) for name in artist_keys(row.get('artist', '').split('+')[0])]
        for key in keys:
            if key in known:
                parents[root(i)] = root(known[key])
            else:
                known[key] = i
    return [f'cross_corpus:{root(i)}' for i in range(len(rows))]


def balanced_folds(rows, groups, count=5, seed=SEED):
    """Keep global components intact and represent every native task per fold."""
    names = sorted({row['task'] for row in rows})
    keys = sorted(set(groups))
    key_indices, name_indices = {key: i for i, key in enumerate(keys)}, {name: i for i, name in enumerate(names)}
    counts = np.zeros((len(keys), len(names)))
    for row, group in zip(rows, groups):
        # Unlabeled audio keeps its component, but cannot establish coverage
        # for a native loss that requires at least one observed target.
        if not row.get('native_label_withheld', False):
            counts[key_indices[group], name_indices[row['task']]] += 1
    if ((counts > 0).sum(0) < count).any():
        raise ValueError('A native task has too few global components for the requested folds')
    scale = counts.sum(0)
    order = np.random.default_rng(seed).permutation(len(keys)).tolist()
    order.sort(key=lambda i: -float((counts[i] / scale).max()))
    totals = np.zeros((count, len(names)))
    allocation = {}
    for i in order:
        costs = (((totals + counts[i]) / scale) ** 2 - (totals / scale) ** 2).sum(1)
        fold = int(costs.argmin())
        allocation[keys[i]] = fold
        totals[fold] += counts[i]
    # A rare task can still miss a fold after balancing larger shared groups.
    # Repair coverage while preserving every other task's current coverage.
    for name_index in range(len(names)):
        coverage = np.array([sum(counts[i, name_index] > 0 and allocation[key] == fold
                                 for i, key in enumerate(keys)) for fold in range(count)])
        for missing in np.flatnonzero(coverage == 0):
            eligible = []
            for i, key in enumerate(keys):
                donor = allocation[key]
                if counts[i, name_index] == 0 or coverage[donor] < 2:
                    continue
                remaining = totals[donor] - counts[i]
                if ((remaining == 0) & (counts[i] > 0)).any():
                    continue
                cost = float((((totals[missing] + counts[i]) / scale) ** 2).sum())
                eligible.append((cost, key, i, donor))
            if not eligible:
                raise ValueError('Cannot retain all native tasks while repairing grouped fold coverage')
            _, key, i, donor = min(eligible)
            allocation[key] = int(missing)
            totals[donor] -= counts[i]
            totals[missing] += counts[i]
            coverage[donor] -= 1
            coverage[missing] += 1
    result = np.array([allocation[group] for group in groups])
    if (totals == 0).any():
        raise ValueError('Native task missing after fold coverage repair')
    return result


def audit_folds(rows, groups, split):
    """Fail before fitting if any outer/inner partition lacks a labeled task."""
    names = sorted({row['task'] for row in rows})
    audit = []

    def counts(selected_rows):
        return {name: sum(row['task'] == name and not row.get('native_label_withheld', False)
                          for row in selected_rows) for name in names}

    for fold in range(5):
        train, valid = split != fold, split == fold
        training_rows = [row for row, keep in zip(rows, train) if keep]
        validation_rows = [row for row, keep in zip(rows, valid) if keep]
        training_groups = np.asarray(groups)[train].tolist()
        inner = balanced_folds(training_rows, training_groups, count=3, seed=SEED + 1)
        partitions = {'outer_train': counts(training_rows), 'outer_valid': counts(validation_rows)}
        for inner_fold in range(3):
            for role, mask in (('train', inner != inner_fold), ('valid', inner == inner_fold)):
                partitions[f'inner_{inner_fold}_{role}'] = counts(
                    [row for row, keep in zip(training_rows, mask) if keep])
        if any(count == 0 for partition in partitions.values() for count in partition.values()):
            raise ValueError('A global partition lacks labeled native targets: ' + str(partitions))
        audit.append({'outer_fold': fold, 'labeled_rows': partitions})
    return audit


def loss_gradient(parameters, z, y, weights, clean=None, augmented=None, strength=0., penalty=0.):
    w1, b1, w2, b2 = [parameters[key] for key in ('shared_weights', 'shared_bias', 'output_weights', 'output_bias')]
    preactivation = z @ w1 + b1
    hidden = np.maximum(preactivation, 0)
    error = hidden @ w2 + b2 - y
    loss = float((error * error * weights).sum())
    delta = 2 * weights * error
    shared = (delta @ w2.T) * (preactivation > 0)
    gradient = {'shared_weights': z.T @ shared, 'shared_bias': shared.sum(0),
                'output_weights': hidden.T @ delta, 'output_bias': delta.sum(0)}
    if strength and clean is not None and len(clean):
        first, second = clean @ w1 + b1, augmented @ w1 + b1
        first_hidden, second_hidden = np.maximum(first, 0), np.maximum(second, 0)
        difference = (first_hidden - second_hidden) @ w2
        loss += float(strength * (difference * difference).mean())
        output_delta = 2 * strength * difference / difference.size
        hidden_delta = output_delta @ w2.T
        first_delta, second_delta = hidden_delta * (first > 0), hidden_delta * (second > 0)
        gradient['shared_weights'] += clean.T @ first_delta - augmented.T @ second_delta
        gradient['shared_bias'] += (first_delta - second_delta).sum(0)
        gradient['output_weights'] += (first_hidden - second_hidden).T @ output_delta
    for key in ('shared_weights', 'output_weights'):
        loss += float(penalty / 2 * (parameters[key] ** 2).sum())
        gradient[key] += penalty * parameters[key]
    return loss, gradient


def training_pairs(rows):
    source = OUT / 'unlabeled-consistency/pairs.json'
    split = json.loads((OUT / 'unlabeled-consistency/pair-split.json').read_text())
    allowed = {row['audio_sha256'] for row in rows}
    heldout_ids = set(split['holdout_ids'])
    unique = {pair['audio_sha256']: pair for pair in json.loads(source.read_text())['pairs']
              if pair['audio_sha256'] in allowed and pair['id'] not in heldout_ids}
    return list(unique.values())


def fit(x, y, rows, offsets, hidden, penalty, strength):
    z, medians, mean, scale = standardize(x)
    rng = np.random.default_rng(SEED)
    parameters = {'shared_weights': rng.normal(0, np.sqrt(2 / x.shape[1]), (x.shape[1], hidden)),
                  'shared_bias': np.full(hidden, .01), 'output_weights': np.zeros((hidden, y.shape[1])),
                  'output_bias': np.zeros(y.shape[1])}
    blocks = {name: np.array([i for i, row in enumerate(rows)
                              if row['task'] == name and not row.get('native_label_withheld', False)])
              for name in offsets}
    if any(len(indices) == 0 for indices in blocks.values()):
        raise ValueError('A native task has no training examples in this global fold')
    for name, indices in blocks.items():
        start, end = offsets[name]
        parameters['output_bias'][start:end] = y[indices, start:end].mean(0)
    pairs = training_pairs(rows) if strength else []
    clean = (np.array([pair['clean'] for pair in pairs]) - mean) / scale if pairs else None
    augmented = (np.array([pair['augmented'] for pair in pairs]) - mean) / scale if pairs else None
    first_moment, second_moment = [{key: np.zeros_like(value) for key, value in parameters.items()} for _ in range(2)]
    trace = []
    for step in range(1, STEPS + 1):
        indices = np.concatenate([rng.choice(block, 32, replace=len(block) < 32) for block in blocks.values()])
        weight = np.zeros((len(indices), y.shape[1]))
        for i, name in enumerate(blocks):
            start, end = offsets[name]
            weight[32 * i:32 * (i + 1), start:end] = 1 / (len(blocks) * 32 * (end - start))
        pair_indices = rng.choice(len(pairs), min(32, len(pairs)), replace=False) if pairs else []
        loss, gradient = loss_gradient(parameters, z[indices], y[indices], weight,
                                       clean[pair_indices] if pairs else None,
                                       augmented[pair_indices] if pairs else None, strength, penalty)
        if not np.isfinite(loss) or not all(np.isfinite(value).all() for value in gradient.values()):
            raise ValueError('Nonfinite shared-model objective')
        norm = np.sqrt(sum(float((value * value).sum()) for value in gradient.values()))
        for key in parameters:
            g = gradient[key] * min(1, 10 / max(norm, 1e-12))
            first_moment[key] = .9 * first_moment[key] + .1 * g
            second_moment[key] = .999 * second_moment[key] + .001 * g * g
            parameters[key] -= .003 * (first_moment[key] / (1 - .9 ** step)) / (np.sqrt(second_moment[key] / (1 - .999 ** step)) + 1e-8)
        if step == 1 or step % 50 == 0:
            trace.append({'step': step, 'batch_objective': loss})
    return {'medians': medians, 'mean': mean, 'scale': scale, 'parameters': parameters,
            'hidden': hidden, 'penalty': penalty, 'consistency_strength': strength,
            'unlabeled_training_recordings': len(pairs), 'training_trace': trace}


def representation(model, x):
    clean = np.where(np.isfinite(x), x, model['medians'])
    p = model['parameters']
    return np.maximum(((clean - model['mean']) / model['scale']) @ p['shared_weights'] + p['shared_bias'], 0)


def prediction(model, x):
    return np.clip(representation(model, x) @ model['parameters']['output_weights'] + model['parameters']['output_bias'], 0, 1)


def native_loss(y, values, rows, offsets):
    losses = []
    for name, (start, end) in offsets.items():
        selected = np.array([row['task'] == name and not row.get('native_label_withheld', False) for row in rows])
        if not selected.any():
            raise ValueError('A native task has no validation examples in a global fold')
        losses.append(float(np.abs(y[selected, start:end] - values[selected, start:end]).mean()))
    return float(np.mean(losses))


def choose(x, y, rows, groups, offsets):
    inner = balanced_folds(rows, groups, count=3, seed=SEED + 1)
    losses = np.zeros(len(GRID))
    for fold in range(3):
        train, valid = inner != fold, inner == fold
        training_rows = [row for row, keep in zip(rows, train) if keep]
        validation_rows = [row for row, keep in zip(rows, valid) if keep]
        for i, (hidden, penalty, strength) in enumerate(GRID):
            model = fit(x[train], y[train], training_rows, offsets, hidden, penalty, strength)
            losses[i] += native_loss(y[valid], prediction(model, x[valid]), validation_rows, offsets)
    choices = {'supervised_shared': min((i for i, key in enumerate(GRID) if key[2] == 0), key=lambda i: losses[i]),
               'unlabeled_shared': int(losses.argmin())}
    return {name: GRID[index] for name, index in choices.items()}, losses.tolist()


def gold_diagnostic(model, names, provenance, variant):
    from .external_binary_calibration import fit as binary_fit, predict as binary_predict, metrics as ranking_metrics, select as binary_select
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    x = np.array([row['embedding_values'] for row in rows])
    y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
    # Match the other gold diagnostics exactly; global native grouping above
    # still retains Unicode artist aliases and cross-corpus audio components.
    groups = [normalize(row['artist']) or row['video_id'] for row in rows]
    split = folds(groups)
    features = {'embedding': x, 'shared_hidden': representation(model, x), 'shared_native_qualities': prediction(model, x)}
    features['embedding_shared_hidden'] = np.column_stack([x, features['shared_hidden']])
    features['embedding_shared_qualities'] = np.column_stack([x, features['shared_native_qualities']])
    results, saved, ranking_results, ranking_saved = {}, {}, {}, {}
    for family, values in features.items():
        fitted_values = np.zeros_like(y)
        for fold in range(5):
            train, valid = split != fold, split == fold
            alpha = select_vibe_alpha(values[train], y[train], np.array(groups)[train].tolist())
            fitted_values[valid] = predict(fit_path(values[train], y[train], [alpha])[0], values[valid])
        results[family] = score(y, fitted_values)
        saved[family] = fitted_values.tolist()
        alpha = select_vibe_alpha(values, y, groups)
        save(OUT / 'models/multitask' / f'{variant}.gold-{family}.json', {
             'format': 'external-research-multitask-vibes-1', 'shared_model_variant': variant,
             'target_names': list(VIBES), 'gold_training_ids': [row['video_id'] for row in rows],
             'native_target_names': names, 'family': family,
             'calibrator': serializable(fit_path(values, y, [alpha])[0]), 'provenance': provenance})
        ranked_values = np.zeros_like(y)
        for fold in range(5):
            train, valid = split != fold, split == fold
            alpha = binary_select(values[train], y[train], np.array(groups)[train].tolist())
            ranked_values[valid] = binary_predict(binary_fit(values[train], y[train], alpha), values[valid])
        ranking_results[family] = ranking_metrics(y, ranked_values)
        ranking_saved[family] = ranked_values.tolist()
        alpha = binary_select(values, y, groups)
        save(OUT / 'models/multitask' / f'{variant}.ranking-{family}.json', {
             'format': 'external-research-multitask-vibe-ranking-1', 'shared_model_variant': variant,
             'target_names': list(VIBES), 'family': family,
             'calibrator': serializable(binary_fit(values, y, alpha)), 'provenance': provenance})
    save(OUT / f'{variant}.gold.report.json', {'created_at': stamp(), 'methods': results, 'ranking_methods': ranking_results,
         'rows': len(rows), 'silver_rows_excluded': len(snapshot['silver']),
         'validation': 'Shared model trained only on external native labels with all known gold identities removed. Five artist-group outer folds and three inner folds fit six-vibe calibration.',
         'snapshot_sha256': digest(SNAPSHOT), 'provenance': provenance,
         'limitations': ['Small, previously examined developer snapshot; feature-family comparisons remain exploratory.',
                        'Shared representation hyperparameters were selected solely by external native validation.',
                        'Binary ranking treats unselected alternatives as comparison negatives; they are not explicit human rejections.',
                        'Class-balanced ranking outputs are not population-calibrated suitability probabilities.']})
    save(OUT / f'{variant}.gold.oof.json', {'ids': [row['video_id'] for row in rows],
         'folds': split.tolist(), 'observed': y.tolist(), 'predictions': saved, 'ranking_predictions': ranking_saved})
    return results


def evaluate(sources):
    with threadpool_limits(limits=1):
        rows, x, y, offsets, names, provenance = load(sources)
        groups = connected_groups(rows)
        split = balanced_folds(rows, groups)
        fold_audit = audit_folds(rows, groups, split)
        save(OUT / 'multitask.fold-audit.json', fold_audit)
        methods = {name: np.zeros_like(y) for name in ('individual_ridge', 'supervised_shared', 'unlabeled_shared')}
        choices = []
        for fold in range(5):
            train, valid = split != fold, split == fold
            training_rows = [row for row, keep in zip(rows, train) if keep]
            chosen, losses = choose(x[train], y[train], training_rows, np.array(groups)[train].tolist(), offsets)
            for method, (hidden, penalty, strength) in chosen.items():
                model = fit(x[train], y[train], training_rows, offsets, hidden, penalty, strength)
                methods[method][valid] = prediction(model, x[valid])
            for source, (start, end) in offsets.items():
                belongs = np.array([row['task'] == source for row in rows])
                labeled = np.array([not row.get('native_label_withheld', False) for row in rows])
                source_train, source_valid = train & belongs & labeled, valid & belongs
                training_groups = np.array(groups)[source_train].tolist()
                alpha = choose_alpha(x[source_train], y[source_train, start:end], training_groups, list(range(end - start)))
                fitted = fit_path(x[source_train], y[source_train, start:end], [alpha])[0]
                methods['individual_ridge'][source_valid, start:end] = predict(fitted, x[source_valid])
            choices.append({'fold': fold, 'chosen': chosen, 'inner_losses': losses})
            print(json.dumps({'shared_model_outer_fold': fold, 'choices': chosen}), flush=True)
        results, withheld_results = {}, {}
        for source, (start, end) in offsets.items():
            selected = np.array([row['task'] == source and not row.get('native_label_withheld', False) for row in rows])
            results[source] = {method: metrics(y[selected, start:end], values[selected, start:end], names[source])
                               for method, values in methods.items()}
            withheld = np.array([row['task'] == source and row.get('native_label_withheld', False) for row in rows])
            if withheld.any():
                withheld_results[source] = {'rows': int(withheld.sum()), 'methods': {
                    method: metrics(y[withheld, start:end], values[withheld, start:end], names[source])
                    for method, values in methods.items()}}
        counts = {source: {'labeled': sum(row['task'] == source and not row['native_label_withheld'] for row in rows),
                           'unlabeled': sum(row['task'] == source and row['native_label_withheld'] for row in rows)}
                  for source in sources}
        report = {'created_at': stamp(), 'sources': sources, 'rows': len(rows), 'rows_by_source': counts,
                  'connected_groups': len(set(groups)),
                  'fold_audit': fold_audit,
                  'native_results': results, 'withheld_native_results': withheld_results,
                  'withheld_evaluation_policy': 'Secondary evaluation against preserved labels withheld from every fit and hyperparameter choice. Outer-fold models also exclude the evaluated audio and its global component; native labels are revealed only for scoring.',
                  'choices_by_fold': choices, 'grid': GRID, 'steps': STEPS,
                  'script_sha256': digest(__file__), 'provenance': provenance,
                  'validation': 'Five global artist/audio connected-component outer folds; three global inner folds choose hidden dimension, L2 and optional consistency. Selection loss weights each native corpus equally and excludes withheld native labels. Baseline ridge uses identical global outer folds and the same labeled training subset. Unlabeled pairs come only from the current training partition.',
                  'limitations': ['Artist aliases and unidentified recordings leave unknown cross-corpus overlaps.',
                                 'Dataset-balanced training gives small corpora equal influence; it does not establish equally reliable targets.',
                                 'Only the small representation and native heads are updated; the MusiCNN backbone is frozen.']}
        save(OUT / 'multitask.report.json', report)
        save(OUT / 'multitask.oof.json', {'rows': [dict(id=row['id'], source=row['task'], group=group, fold=int(fold), native_label_withheld=row['native_label_withheld'])
             for row, group, fold in zip(rows, groups, split)], 'target_names': names, 'offsets': offsets,
             'observed': y.tolist(), 'predictions': {name: value.tolist() for name, value in methods.items()}})
        chosen, losses = choose(x, y, rows, groups, offsets)
        for method, (hidden, penalty, strength) in chosen.items():
            model = fit(x, y, rows, offsets, hidden, penalty, strength)
            variant = 'multitask-' + method
            save(OUT / 'models/multitask' / f'{variant}.json', {
                 'format': 'external-research-multitask-1', 'model': serializable(model),
                 'sources': sources, 'target_names': names, 'offsets': offsets, 'provenance': provenance,
                 'training_ids': [row['id'] for row in rows], 'inner_losses': losses,
                 'script_sha256': digest(__file__)})
            gold_diagnostic(model, names, provenance, variant)
        return report
