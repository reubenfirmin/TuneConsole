"""Native-label research baselines with nested, grouped cross-validation.

Artifacts here are research models, never application model replacements.
"""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.stats import rankdata
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, save, stamp

ALPHAS = (0.1, 1., 10., 100., 1000.)
SEED = 507


def folds(groups, count=5, seed=SEED):
    """Keep related examples together; balance by group size, independent of y."""
    sizes = Counter(groups)
    if len(sizes) < count:
        raise ValueError(f'Need {count} independent groups; found {len(sizes)}')
    rng = np.random.default_rng(seed)
    keys = sorted(sizes)
    rng.shuffle(keys)
    keys.sort(key=lambda key: -sizes[key])
    totals = [0] * count
    allocation = {}
    for key in keys:
        slot = int(np.argmin(totals))
        allocation[key] = slot
        totals[slot] += sizes[key]
    return np.array([allocation[group] for group in groups])


def standardize(x):
    x = np.asarray(x, dtype=float)
    medians = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0
                        for col in x.T])
    clean = np.where(np.isfinite(x), x, medians)
    mean = clean.mean(axis=0)
    scale = clean.std(axis=0)
    scale[scale < 1e-9] = 1
    return (clean - mean) / scale, medians, mean, scale


def fit_path(x, y, alphas=ALPHAS):
    z, medians, mean, scale = standardize(x)
    target_mean = y.mean(axis=0)
    u, singular, vt = np.linalg.svd(z, full_matrices=False)
    projected = u.T @ (y - target_mean)
    return [{'medians': medians, 'mean': mean, 'scale': scale,
             'weights': (vt.T * (singular / (singular ** 2 + alpha))) @ projected,
             'intercept': target_mean, 'alpha': alpha} for alpha in alphas]


def predict(model, x):
    clean = np.where(np.isfinite(x), x, model['medians'])
    return np.clip(((clean - model['mean']) / model['scale']) @ model['weights'] + model['intercept'], 0, 1)


def choose_alpha(x, y, groups, selection):
    inner = folds(groups, count=3, seed=SEED + 1)
    losses = np.zeros(len(ALPHAS))
    for fold in range(3):
        train, valid = inner != fold, inner == fold
        for index, model in enumerate(fit_path(x[train], y[train])):
            losses[index] += np.abs(predict(model, x[valid])[:, selection] - y[valid][:, selection]).sum()
    return ALPHAS[int(np.argmin(losses))]


def correlations(y, prediction, ranked=False):
    result = []
    for observed, fitted in zip(y.T, prediction.T):
        if ranked:
            observed, fitted = rankdata(observed), rankdata(fitted)
        if np.std(observed) < 1e-12 or np.std(fitted) < 1e-12:
            result.append(None)
        else:
            result.append(float(np.corrcoef(observed, fitted)[0, 1]))
    return result


def metrics(y, prediction, names, selection=None):
    error = np.abs(y - prediction)
    pearson, spearman = correlations(y, prediction), correlations(y, prediction, True)
    use = list(range(len(names))) if selection is None else selection
    def average(values):
        selected = [values[i] for i in use if values[i] is not None]
        return float(np.mean(selected)) if selected else None
    return {'mae': float(error[:, use].mean()),
            'mse': float(((y[:, use] - prediction[:, use]) ** 2).mean()), 'pearson': average(pearson),
            'spearman': average(spearman),
            'targets': {name: {'mae': float(error[:, i].mean()), 'pearson': pearson[i],
                               'spearman': spearman[i]} for i, name in enumerate(names)}}


def feature_matrices(rows):
    results = [json.loads(Path(row['feature_file']).read_text()) for row in rows]
    attribute_names = sorted(results[0]['audio_attributes']['values'])
    mood_names = sorted(results[0]['prediction']['moods']['traits'])
    attributes = np.array([[result['audio_attributes']['values'].get(key, np.nan)
                            for key in attribute_names] for result in results], dtype=float)
    embedding = np.array([result['prediction']['embedding']['values'] for result in results])
    moods = []
    for result in results:
        traits = result['prediction']['moods']['traits']
        moods.append([(traits[key]['score'] - traits[key]['range'][0]) /
                      (traits[key]['range'][1] - traits[key]['range'][0]) for key in mood_names])
    moods = np.array(moods)
    families = {'attributes': attributes, 'embedding': embedding, 'pretrained_moods': moods,
                'attributes_embedding': np.column_stack([attributes, embedding]),
                'attributes_embedding_moods': np.column_stack([attributes, embedding, moods])}
    feature_names = {'attributes': attribute_names,
                     'embedding': [f'musicnn_{i:03d}' for i in range(embedding.shape[1])],
                     'pretrained_moods': mood_names}
    feature_names['attributes_embedding'] = attribute_names + feature_names['embedding']
    feature_names['attributes_embedding_moods'] = feature_names['attributes_embedding'] + mood_names
    return families, feature_names, moods, mood_names


def bootstrap_improvement(y, baseline, prediction, groups, selection):
    """Cluster bootstrap of paired OOF losses; model selection is not repeated."""
    delta = (np.abs(y - baseline) - np.abs(y - prediction))[:, selection].mean(axis=1)
    keys = sorted(set(groups))
    sums = np.array([delta[np.array(groups) == group].sum() for group in keys])
    sizes = np.array([sum(g == group for g in groups) for group in keys])
    draw = np.random.default_rng(SEED).integers(len(keys), size=(2000, len(keys)))
    values = sums[draw].sum(axis=1) / sizes[draw].sum(axis=1)
    return {'mae_reduction': float(delta.mean()), 'conditional_95_percent_interval':
            np.quantile(values, [.025, .975]).tolist(),
            'method': 'paired group bootstrap of fixed out-of-fold predictions; selection uncertainty excluded'}


def evaluate(dataset):
    with threadpool_limits(limits=1):
        return _evaluate(dataset)


def _evaluate(dataset):
    source_path = OUT / f'{dataset}.features.json'
    source = json.loads(source_path.read_text())
    rows = source['rows']
    if len(rows) < 30:
        raise ValueError('Not enough successfully extracted examples')
    names = rows[0]['target_names']
    if any(row['target_names'] != names for row in rows):
        raise ValueError('Target order differs between rows')
    # Fractional predictions must remain floating-point even for one-hot labels.
    y = np.array([row['targets'] for row in rows], dtype=float)
    groups = [row['group'] for row in rows]
    outer = folds(groups)
    families, feature_names, moods, mood_names = feature_matrices(rows)
    if not all(np.isfinite(x).all() for name, x in families.items() if name == 'embedding'):
        raise ValueError('Nonfinite embeddings')
    # CAL500's instrument and genre labels are retained, but do not choose the
    # regularization strength for an emotion/activity experiment.
    selection = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
    if not selection:
        selection = list(range(len(names)))
    baseline = np.zeros_like(y)
    median_baseline = np.zeros_like(y)
    for fold in range(5):
        train, test = outer != fold, outer == fold
        assert set(np.array(groups)[train]).isdisjoint(set(np.array(groups)[test]))
        baseline[test] = y[train].mean(axis=0)
        median_baseline[test] = np.median(y[train], axis=0)
    report = {'created_at': stamp(), 'dataset': dataset, 'rows': len(rows),
              'unique_groups': len(set(groups)), 'extraction_errors': source['errors'],
              'source_sha256': digest(source_path), 'trainer_sha256': digest(__file__),
              'target_names': names, 'selection_targets': [names[i] for i in selection],
              'validation': {'outer_group_folds': 5, 'inner_group_folds': 3, 'seed': SEED,
                             'alphas': ALPHAS, 'preprocessing': 'train-only median imputation and standardization'},
              'baseline': metrics(y, baseline, names, selection),
              'median_baseline': metrics(y, median_baseline, names, selection), 'families': {},
              'limitations': [
                  'One center ten-second excerpt per recording; excerpt labels may describe longer audio.',
                  'Pretrained embedding/head upstream training overlap is not fully known.',
                  'Within-dataset validation is not evidence of transfer to the six product vibes.',
                  'No regular-user vibe labels, product database writes, or active model changes.']}
    if dataset == 'soundtracks-all':
        report['limitations'].append('Set 1 pilot labels have fewer raters; unlinked Set 1 films lack identity metadata and cannot be fully film-grouped.')
    if dataset == 'emotify':
        report['limitations'].append('Grouping is by recording: source does not supply artist identities. Labels concern induced emotion.')
    if dataset == 'pmemo':
        report['limitations'].append('Grouping uses primary artist before collaborative credits; guest-artist overlap can remain.')
    if dataset == 'emma-preview-matched':
        report['limitations'].append('Current public previews are about 15 seconds and have unverified alignment to the original longer rated excerpts. This tests weak supervision on available previews, not the original EMMA benchmark. Frozen current GEMS-45 export; liking and familiarity excluded.')
    predictions = {'training_mean': baseline, 'training_median': median_baseline}
    for family, x in families.items():
        oof = np.zeros_like(y)
        chosen = []
        for fold in range(5):
            train, test = outer != fold, outer == fold
            alpha = choose_alpha(x[train], y[train], np.array(groups)[train].tolist(), selection)
            model = fit_path(x[train], y[train], [alpha])[0]
            oof[test] = predict(model, x[test])
            chosen.append(alpha)
        score = metrics(y, oof, names, selection)
        score['alpha_by_fold'] = chosen
        score['versus_training_mean'] = bootstrap_improvement(y, baseline, oof, groups, selection)
        score['versus_training_median'] = bootstrap_improvement(y, median_baseline, oof, groups, selection)
        if family != 'attributes':
            score['versus_attributes'] = bootstrap_improvement(y, predictions['attributes'], oof, groups, selection)
        report['families'][family] = score
        predictions[family] = oof
        # Full-data model is saved separately from the OOF evidence.
        alpha = choose_alpha(x, y, groups, selection)
        model = fit_path(x, y, [alpha])[0]
        save(OUT / 'models' / f'{dataset}.{family}.json', {
            'format': 'external-research-ridge-1', 'dataset': dataset, 'family': family,
            'target_names': names, 'feature_names': feature_names[family],
            'training_ids': [row['id'] for row in rows], 'source_sha256': digest(source_path),
            'extractor_signatures': sorted({row['extractor_signature'] for row in rows}),
            **{key: value.tolist() if isinstance(value, np.ndarray) else value for key, value in model.items()}})
        print(json.dumps({'dataset': dataset, 'family': family, 'rows': len(rows),
                          'mae': score['mae'], 'baseline_mae': report['baseline']['mae']}), flush=True)
    # Direct pretrained predictions are evaluated only where scales/constructs
    # correspond. Arousal versus soundtrack energy is explicitly a proxy.
    direct = {}
    for i, target in enumerate(names):
        key = 'arousal' if target == 'energy' else target
        if key in mood_names:
            direct[target] = metrics(y[:, i:i+1], moods[:, mood_names.index(key):mood_names.index(key)+1], [target])
            direct[target]['proxy'] = target == 'energy'
    report['direct_pretrained'] = direct
    save(OUT / f'{dataset}.splits.json', [dict(id=row['id'], group=row['group'], fold=int(fold))
                                         for row, fold in zip(rows, outer)])
    with (OUT / f'{dataset}.oof.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'group', 'fold', 'target', 'observed', *predictions])
        for i, row in enumerate(rows):
            for j, target in enumerate(names):
                writer.writerow([row['id'], row['group'], outer[i], target, y[i, j],
                                 *[value[i, j] for value in predictions.values()]])
    # Main table keeps native labels separate rather than merging unlike tasks.
    save(OUT / f'{dataset}.report.json', report)
    markdown = [f'# {dataset}: offline native-label experiment', '',
                f'{len(rows)} audio examples; {len(set(groups))} groups; nested 5/3 grouped folds.', '',
                'All targets are normalized to their native 0–1 scale. Lower MAE is better.', '',
                '| Features | MAE | Pearson | MAE reduction versus training mean |',
                '| --- | ---: | ---: | ---: |',
                f"| Training mean | {report['baseline']['mae']:.4f} | — | — |",
                f"| Training median | {report['median_baseline']['mae']:.4f} | — | — |"]
    for family, score in report['families'].items():
        pearson = 'undefined' if score['pearson'] is None else f"{score['pearson']:.3f}"
        markdown.append(f"| {family} | {score['mae']:.4f} | {pearson} | {score['versus_training_mean']['mae_reduction']:.4f} |")
    markdown.extend(['', 'Limitations:', '', *['- ' + value for value in report['limitations']], ''])
    (OUT / f'{dataset}.report.md').write_text('\n'.join(markdown))
    return report
