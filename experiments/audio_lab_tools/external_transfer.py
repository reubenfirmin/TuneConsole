"""Evaluate frozen external emotion heads against the existing vibe snapshot.

Gold remains human-labeled; the silver prediction set is never promoted to gold.
This is a small developer diagnostic, not a product-wide acceptance test.
"""
import json
from pathlib import Path
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import LAB, OUT, digest, normalize, save, stamp
from .external_training import ALPHAS, SEED, choose_alpha, fit_path, folds, predict

SNAPSHOT = LAB / 'local/vibe-gold-silver-20261028T010856Z-evaluation.dataset.json'
VIBES = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')


def probabilities(values):
    values = np.maximum(values, 1e-8)
    return values / values.sum(axis=1, keepdims=True)


def score(targets, predictions):
    p = probabilities(predictions)
    accepted = targets > 0
    return {'top_1_acceptable': float(accepted[np.arange(len(p)), p.argmax(axis=1)].mean()),
            'acceptable_log_loss': float(-np.log((p * accepted).sum(axis=1)).mean()),
            'rows': len(p)}


def select_vibe_alpha(x, y, groups):
    split = folds(groups, count=3, seed=SEED + 1)
    loss = np.zeros(len(ALPHAS))
    for fold in range(3):
        train, test = split != fold, split == fold
        for i, model in enumerate(fit_path(x[train], y[train])):
            loss[i] += score(y[test], predict(model, x[test]))['acceptable_log_loss'] * sum(test)
    return ALPHAS[int(loss.argmin())]


def evaluate_transfer(datasets):
    with threadpool_limits(limits=1):
        return _evaluate_transfer(datasets)


def external_head_features(datasets, gold):
    """Freeze native heads, excluding identifiable gold overlaps before fitting."""
    embeddings = np.array([row['embedding_values'] for row in gold])
    if embeddings.shape != (len(gold), 200) or not np.isfinite(embeddings).all():
        raise ValueError('Gold snapshot does not contain compatible 200-dimensional embeddings')
    gold_artists = {normalize(row['artist']) for row in gold if row['artist']}
    gold_titles = {normalize(row['title']) for row in gold if row['title']}
    external_features, external_names, overlaps, signatures, frozen_heads = [], [], {}, set(), {}
    for dataset in datasets:
        model_path = OUT / 'models' / f'{dataset}.embedding.json'
        model = json.loads(model_path.read_text())
        index = json.loads((OUT / f'{dataset}.features.json').read_text())['rows']
        # Verify upstream representation identity, including actual weight hashes.
        from yt_playlist.core.audio_embedding import signature
        example = json.loads(Path(index[0]['feature_file']).read_text())['prediction']
        native_signature = signature({**example, 'artifact_hashes': {
            item['path']: item['sha256'] for item in example['bundle']['files']}})
        signatures.add(native_signature)
        if any(row['embedding_signature'] != native_signature for row in gold):
            raise ValueError('External and gold embeddings use different model provenance')
        excluded = [row for row in index if (normalize(row['artist']) in gold_artists
                    or normalize(row['title']) in gold_titles)]
        overlaps[dataset] = [row['id'] for row in excluded]
        if excluded:
            excluded_ids = {row['id'] for row in excluded}
            retained = [row for row in index if row['id'] not in excluded_ids]
            x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values']
                          for row in retained])
            y = np.array([row['targets'] for row in retained])
            names = model['target_names']
            selection = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
            alpha = choose_alpha(x, y, [row['group'] for row in retained], selection or list(range(len(names))))
            fitted = fit_path(x, y, [alpha])[0]
        else:
            fitted = {key: np.array(model[key]) for key in ['medians', 'mean', 'scale', 'weights', 'intercept']}
        names = model['target_names']
        use = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
        use = use or list(range(len(names)))
        external_features.append(predict(fitted, embeddings)[:, use])
        external_names.extend(f'{dataset}:{names[i]}' for i in use)
        frozen_heads[dataset] = {'target_names': names, 'selected_columns': use,
                                'source_model_sha256': digest(model_path),
                                'excluded_ids': overlaps[dataset],
                                'model': {key: value.tolist() if isinstance(value, np.ndarray) else value
                                          for key, value in fitted.items()}}
    qualities = np.column_stack(external_features)
    return qualities, external_names, overlaps, signatures, frozen_heads


def _evaluate_transfer(datasets):
    snapshot = json.loads(SNAPSHOT.read_text())
    gold = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    excluded_gold = [row['video_id'] for row in snapshot['gold'] if len(row.get('embedding_values', [])) != 200]
    embeddings = np.array([row['embedding_values'] for row in gold])
    groups = [normalize(row['artist']) or row['video_id'] for row in gold]
    target = np.array([[int(vibe in row['labels']) / len(row['labels']) for vibe in VIBES] for row in gold])
    outer = folds(groups)
    qualities, external_names, overlaps, signatures, _ = external_head_features(datasets, gold)
    families = {'embedding': embeddings, 'external_qualities': qualities,
                'embedding_external_qualities': np.column_stack([embeddings, qualities])}
    base = np.zeros_like(target)
    for fold in range(5):
        base[outer == fold] = target[outer != fold].mean(axis=0)
    heuristic = np.array([[row['baseline'][vibe] for vibe in VIBES] for row in gold])
    teacher_unseen = np.array([row['video_id'] not in snapshot['teacher']['training_video_ids'] for row in gold])
    report = {'created_at': stamp(), 'datasets': datasets, 'gold_rows': len(gold),
              'gold_rows_without_embeddings': excluded_gold,
              'silver_rows_excluded': len(snapshot['silver']), 'vibes': VIBES,
              'snapshot_sha256': digest(SNAPSHOT), 'embedding_signatures': sorted(signatures),
              'external_identity_exclusions': overlaps, 'external_feature_names': external_names,
              'folds': [{'video_id': row['video_id'], 'artist_group': group, 'fold': int(fold)}
                        for row, group, fold in zip(gold, groups, outer)],
              'baselines': {'training_mean': score(target, base), 'heuristic': score(target, heuristic)},
              'families': {}, 'limitations': [
                  f'Only {len(gold)} developer-labeled songs with embeddings; insufficient to establish product-wide quality.',
                  'Gold labels and the six vibe meanings are unchanged; silver labels are not training targets.',
                  'Outer folds separate artist strings; aliases and collaborations can still overlap.',
                  'External title/artist matches are excluded before fitting external transfer heads.',
                  'Unknown overlaps in original pretrained models remain possible.',
                  'External corpus uses one excerpt; gold uses averages of available song excerpts.',
                  'Teacher-unseen subset is descriptive; this snapshot has been used in previous experiments.']}
    for name, x in families.items():
        predictions = np.zeros_like(target)
        alphas = []
        for fold in range(5):
            train, test = outer != fold, outer == fold
            assert set(np.array(groups)[train]).isdisjoint(set(np.array(groups)[test]))
            alpha = select_vibe_alpha(x[train], target[train], np.array(groups)[train].tolist())
            predictions[test] = predict(fit_path(x[train], target[train], [alpha])[0], x[test])
            alphas.append(alpha)
        report['families'][name] = {**score(target, predictions), 'alpha_by_fold': alphas,
                                    'teacher_unseen_subset': score(target[teacher_unseen], predictions[teacher_unseen])}
    slug = '+'.join(datasets)
    save(OUT / f'transfer-{slug}.report.json', report)
    print(json.dumps({'transfer_datasets': datasets, 'families': report['families']}), flush=True)
    return report


def watch():
    """Recover transfer after native reports, independently of old runner imports.

    The initial live suite loaded the transfer module before the missing-gold-
    embedding fix. Preserve its error record and write corrected results here.
    """
    previous, last_attempt, outcomes = None, 0., []
    while True:
        status_path = OUT / 'suite-status.json'
        if status_path.exists():
            status = json.loads(status_path.read_text())
            completed = [row['dataset'] for row in status['datasets']
                         if row['state'] == 'complete' and row['dataset'] != 'soundtracks-all']
            fingerprint = tuple(completed)
            if completed and fingerprint != previous and time.monotonic() - last_attempt > 30:
                last_attempt = time.monotonic()
                try:
                    evaluate_transfer(completed)
                    previous = fingerprint
                    outcomes.append({'datasets': completed, 'state': 'complete', 'finished_at': stamp()})
                except Exception as error:
                    outcomes.append({'datasets': completed, 'state': 'failed', 'error': repr(error),
                                     'finished_at': stamp()})
                    print(json.dumps(outcomes[-1]), flush=True)
            save(OUT / 'transfer-status.json', {'updated_at': stamp(), 'outcomes': outcomes,
                 'state': 'complete' if status['state'].startswith('complete') and fingerprint == previous else 'watching',
                 'note': 'Separate recovery after excluding the one gold row without an embedding; native suite error history is retained.'})
            if status['state'].startswith('complete') and fingerprint == previous:
                return
        time.sleep(15)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('datasets', nargs='*', default=['soundtracks-set2'])
    args = parser.parse_args()
    if args.watch:
        watch()
    else:
        evaluate_transfer(args.datasets or ['soundtracks-set2'])
