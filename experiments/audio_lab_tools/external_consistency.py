"""Research-only learning from unlabeled audio, without pseudo-label targets.

Use real audio augmentation pairs to penalize changes in a predictor's output.
The ordinary human-annotated external training objective remains its anchor.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import LAB, OUT, ROOT, digest, normalize, pcm, save, stamp
from .external_training import (ALPHAS, SEED, bootstrap_improvement, folds,
                                metrics, predict, standardize)
from .external_transfer import SNAPSHOT, VIBES, score

CONSISTENCY = OUT / 'unlabeled-consistency'
LAMBDAS = (0., .1, 1., 10.)
POLICY = 'same-excerpt-attenuation3to9db-snr35db-1'


def wait_for_suite():
    while True:
        path = OUT / 'suite-status.json'
        if path.exists():
            status = json.loads(path.read_text())
            if status['state'].startswith('complete'):
                return
            progress_files = [path, *OUT.glob('*.status.json')]
            if time.time() - max(file.stat().st_mtime for file in progress_files) > 900:
                raise RuntimeError('Native suite has not reported progress for fifteen minutes')
        time.sleep(15)


def unlabeled_pool(limit):
    # Read identities, paths, and previously extracted embeddings. Annotation
    # target values and all saved prediction scores are deliberately unused.
    pmemo = json.loads((OUT / 'pmemo.features.json').read_text())['rows']
    cal = json.loads((OUT / 'cal500.features.json').read_text())['rows']
    gold = json.loads(SNAPSHOT.read_text())['gold']
    exclude = cal + gold
    artists = {normalize(row['artist'].split('+')[0]) for row in exclude if row.get('artist')}
    titles = {normalize(row['title']) for row in exclude if row.get('title')}
    hashes = {row['audio_sha256'] for row in cal}
    eligible, excluded = [], []
    for row in pmemo:
        artist = normalize(row['artist'].split('+')[0])
        if not artist or artist in artists or normalize(row['title']) in titles or row['audio_sha256'] in hashes:
            excluded.append(row['id'])
            continue
        eligible.append({key: row[key] for key in ['id', 'artist', 'title', 'audio', 'audio_sha256',
                                                   'feature_file', 'sample_start', 'extractor_signature']})
    # Selection uses a seeded content hash, with no emotion or vibe labels.
    eligible.sort(key=lambda row: hashlib.sha256((str(SEED) + row['audio_sha256']).encode()).hexdigest())
    unique = {}
    for row in eligible:
        unique.setdefault(row['audio_sha256'], row)
    selected = list(unique.values())[:limit]
    save(CONSISTENCY / 'pool.json', {'created_at': stamp(), 'seed': SEED,
         'eligible_count': len(eligible), 'unique_recordings': len(unique), 'requested': limit, 'selected': selected,
         'excluded_ids': excluded, 'policy': 'unlabeled PMEmo; exact artist/title/audio overlap exclusions against CAL500 and all gold'})
    return selected


def collect_pairs(limit=128, *, rows=None):
    CONSISTENCY.mkdir(parents=True, exist_ok=True)
    rows = unlabeled_pool(limit) if rows is None else rows[:limit]
    environment = dict(os.environ, EXTERNAL_TFJS_NATIVE='1', TF_NUM_INTRAOP_THREADS='1',
                       TF_NUM_INTEROP_THREADS='1')
    provenance = {'policy': POLICY, 'harness_sha256': digest(ROOT / 'experiments/audio_lab_tools/external_audio_worker.mjs'),
                  'script_sha256': digest(__file__), 'native_package_sha256': digest(LAB / 'local/node/package-lock.json')}
    signature = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    cache = CONSISTENCY / 'pairs'
    cache.mkdir(exist_ok=True)
    scratch = CONSISTENCY / 'augmented.f32'
    pairs, failures = [], []
    started = time.monotonic()
    with (CONSISTENCY / 'worker.log').open('a') as log:
        worker = subprocess.Popen(['nice', '-n', '10', 'node', str(ROOT / 'experiments/audio_lab_tools/external_audio_worker.mjs')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                  text=True, bufsize=1, env=environment)
        try:
            for row in rows:
                key = hashlib.sha256((row['audio_sha256'] + row['extractor_signature'] + signature).encode()).hexdigest()
                destination = cache / (key + '.json')
                try:
                    if destination.exists():
                        pair = json.loads(destination.read_text())
                    else:
                        clean = json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values']
                        pcm(row['audio'], row['sample_start'], 16000, 1, scratch)
                        samples = np.fromfile(scratch, dtype='<f4')
                        rng = np.random.default_rng(int(row['audio_sha256'][:16], 16) ^ SEED)
                        gain_db = float(rng.uniform(-9, -3))
                        signal_rms = float(np.sqrt(np.mean(samples.astype(float) ** 2)))
                        if signal_rms < 1e-8:
                            raise ValueError('Silent unlabeled excerpt')
                        noise = rng.normal(0, signal_rms * 10 ** (-35 / 20), size=samples.shape)
                        augmented = ((samples + noise) * 10 ** (gain_db / 20)).astype('<f4')
                        augmented.tofile(scratch)
                        worker.stdin.write(json.dumps({'id': row['id'], 'model_pcm': str(scratch.resolve())}) + '\n')
                        worker.stdin.flush()
                        while True:
                            if not select.select([worker.stdout], [], [], 120)[0]:
                                raise RuntimeError('Augmentation worker response deadline exceeded')
                            line = worker.stdout.readline()
                            if not line:
                                raise RuntimeError('Augmentation worker exited')
                            if line.startswith('RESULT '):
                                result = json.loads(line[7:])
                                break
                        if result.get('error'):
                            raise ValueError(result['error'])
                        transformed = result['prediction']['embedding']['values']
                        if len(clean) != 200 or len(transformed) != 200 or not np.isfinite([clean, transformed]).all():
                            raise ValueError('Invalid augmentation embedding')
                        pair = {'id': row['id'], 'artist': row['artist'], 'audio_sha256': row['audio_sha256'],
                                'clean': clean, 'augmented': transformed, 'gain_db': gain_db, 'noise_snr_db': 35,
                                'extractor_signature': row['extractor_signature'], 'augmentation_signature': signature}
                        save(destination, pair)
                    pairs.append(pair)
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                    failures.append({'id': row['id'], 'error': repr(error)})
                    if worker.poll() is not None:
                        raise
                if len(pairs) % 10 == 0 or len(pairs) == 1:
                    save(CONSISTENCY / 'status.json', {'state': 'extracting', 'pairs': len(pairs),
                         'requested': len(rows), 'errors': failures, 'updated_at': stamp(),
                         'elapsed_seconds': time.monotonic() - started})
                    print(json.dumps({'unlabeled_pairs': len(pairs), 'requested': len(rows), 'errors': len(failures)}), flush=True)
        finally:
            try:
                worker.stdin.close()
            except BrokenPipeError:
                pass
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.terminate()
                worker.wait(timeout=10)
            scratch.unlink(missing_ok=True)
    save(CONSISTENCY / 'pairs.json', {'created_at': stamp(), 'pairs': pairs, 'errors': failures,
                                     'provenance': provenance})
    return pairs


def fit_path(x, y, differences, strengths):
    z, medians, mean, scale = standardize(x)
    target_mean = y.mean(axis=0)
    cross = z.T @ (y - target_mean)
    gram = z.T @ z
    d = differences / scale
    consistency = len(x) / len(d) * (d.T @ d)
    result = []
    for strength in strengths:
        eigenvalues, eigenvectors = np.linalg.eigh(gram + strength * consistency)
        projected = eigenvectors.T @ cross
        for alpha in ALPHAS:
            weights = eigenvectors @ (projected / (np.maximum(eigenvalues, 0) + alpha)[:, None])
            result.append({'medians': medians, 'mean': mean, 'scale': scale, 'intercept': target_mean,
                           'weights': weights, 'alpha': alpha, 'consistency_strength': strength})
    return result


def choose_parameters(x, y, groups, differences, selection, strengths, vibe=False):
    split = folds(groups, count=3, seed=SEED + 1)
    losses = np.zeros(len(ALPHAS) * len(strengths))
    for fold in range(3):
        train, valid = split != fold, split == fold
        models = fit_path(x[train], y[train], differences, strengths)
        for i, model in enumerate(models):
            predicted = predict(model, x[valid])
            losses[i] += (score(y[valid], predicted)['acceptable_log_loss'] * sum(valid) if vibe else
                           np.abs(predicted[:, selection] - y[valid][:, selection]).sum())
    i = int(losses.argmin())
    return strengths[i // len(ALPHAS)], ALPHAS[i % len(ALPHAS)]


def compare(dataset, pairs, holdout_pairs=(), *, output_dir=None):
    destination = CONSISTENCY if output_dir is None else Path(output_dir)
    pair_hashes = [pair['audio_sha256'] for pair in pairs]
    holdout_hashes = [pair['audio_sha256'] for pair in holdout_pairs]
    if set(pair_hashes) & set(holdout_hashes):
        raise ValueError('Unlabeled training and holdout recordings overlap')
    vibe = dataset == 'gold-vibes'
    if vibe:
        snapshot = json.loads(SNAPSHOT.read_text())
        rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
        x = np.array([row['embedding_values'] for row in rows])
        y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
        names, groups = list(VIBES), [normalize(row['artist']) for row in rows]
    else:
        rows = json.loads((OUT / f'{dataset}.features.json').read_text())['rows']
        x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values'] for row in rows])
        y = np.array([row['targets'] for row in rows])
        names, groups = rows[0]['target_names'], [row['group'] for row in rows]
    differences = np.array([np.array(pair['clean']) - pair['augmented'] for pair in pairs])
    if len(differences) < 30:
        raise ValueError('Need at least thirty usable unlabeled pairs')
    selected = [i for i, name in enumerate(names) if name.startswith(('Emotion-', 'Usage-'))]
    selected = selected or list(range(len(names)))
    split = folds(groups)
    all_predictions, parameters, robustness = {}, {}, {}
    for method, strengths in [('ordinary_ridge', (0.,)), ('unlabeled_consistency', LAMBDAS)]:
        predictions = np.zeros_like(y)
        chosen = []
        for fold in range(5):
            train, test = split != fold, split == fold
            strength, alpha = choose_parameters(x[train], y[train], np.array(groups)[train].tolist(),
                                                differences, selected, strengths, vibe)
            model = next(model for model in fit_path(x[train], y[train], differences, [strength]) if model['alpha'] == alpha)
            predictions[test] = predict(model, x[test])
            chosen.append({'alpha': alpha, 'consistency_strength': strength})
        all_predictions[method], parameters[method] = predictions, chosen
        strength, alpha = choose_parameters(x, y, groups, differences, selected, strengths, vibe)
        fitted = next(model for model in fit_path(x, y, differences, [strength]) if model['alpha'] == alpha)
        if holdout_pairs:
            clean = predict(fitted, np.array([pair['clean'] for pair in holdout_pairs]))
            augmented = predict(fitted, np.array([pair['augmented'] for pair in holdout_pairs]))
            robustness[method] = {'recordings': len(holdout_pairs),
                                  'mean_absolute_prediction_change': float(np.abs(clean - augmented).mean()),
                                  'mean_clean_prediction_std': float(clean.std(axis=0).mean()),
                                  'mean_augmented_prediction_std': float(augmented.std(axis=0).mean())}
            if vibe:
                robustness[method]['top_choice_agreement'] = float((clean.argmax(axis=1) == augmented.argmax(axis=1)).mean())
        save(destination / 'models' / f'{dataset}.{method}.json', {
             'format': 'external-research-consistency-ridge-1', 'dataset': dataset,
             'target_names': names, 'embedding_dimensions': 200,
             'pair_source_sha256': digest(CONSISTENCY / 'pairs.json'),
             'training_ids': [row.get('id', row.get('video_id')) for row in rows],
             **{key: value.tolist() if isinstance(value, np.ndarray) else value for key, value in fitted.items()}})
    results = {key: (score(y, p) if vibe else metrics(y, p, names, selected)) for key, p in all_predictions.items()}
    report = {'created_at': stamp(), 'dataset': dataset, 'labeled_rows': len(rows), 'unlabeled_pairs': len(pairs),
              'methods': results, 'parameters_by_fold': parameters, 'target_names': names,
              'variant': destination.name, 'unique_unlabeled_recordings': len(set(pair_hashes)),
              'unique_holdout_recordings': len(set(holdout_hashes)),
              'unlabeled_holdout_robustness': robustness,
              'source_pair_sha256': digest(CONSISTENCY / 'pairs.json'), 'script_sha256': digest(__file__),
              'objective': 'supervised squared loss + alpha coefficient penalty + lambda*n/u summed squared prediction changes on unlabeled pairs',
              'limitations': ['Same-excerpt gain/noise consistency only; no distant sections are forced to match.',
                              'No pseudo-labels or model outputs are used as supervision.',
                              'This is a frozen-embedding predictor update; the MusiCNN backbone is unchanged.',
                              'Known artist/title/audio overlaps with CAL500 and all gold were excluded from the unlabeled pool.',
                              'Small developer vibe evaluation and unknown upstream pretraining overlap remain limitations.']}
    if not vibe:
        report['versus_ordinary_ridge'] = bootstrap_improvement(y, all_predictions['ordinary_ridge'],
                                                              all_predictions['unlabeled_consistency'], groups, selected)
    else:
        unseen = np.array([row['video_id'] not in snapshot['teacher']['training_video_ids'] for row in rows])
        report['teacher_unseen_subset'] = {name: score(y[unseen], p[unseen]) for name, p in all_predictions.items()}
    save(destination / f'{dataset}.report.json', report)
    save(destination / f'{dataset}.oof.json', {'target_names': names, 'observed': y.tolist(),
         'rows': [dict(id=row.get('id', row.get('video_id')), group=group, fold=int(fold))
                  for row, group, fold in zip(rows, groups, split)],
         'predictions': {key: value.tolist() for key, value in all_predictions.items()}})
    print(json.dumps({'consistency_dataset': dataset,
                      'methods': {key: {k: v for k, v in value.items() if k != 'targets'} for key, value in results.items()}}), flush=True)
    return report


def compare_deduplicated():
    """Preserve the original trial and repeat with one contribution per audio."""
    source = json.loads((CONSISTENCY / 'pairs.json').read_text())
    split = json.loads((CONSISTENCY / 'pair-split.json').read_text())
    held_out = set(split['holdout_ids'])
    training_ids = set(split['training_ids'])
    unique, duplicated = {}, []
    for pair in source['pairs']:
        if pair['audio_sha256'] in unique:
            duplicated.append(pair['id'])
        else:
            unique[pair['audio_sha256']] = pair
    train = [pair for pair in unique.values() if pair['id'] in training_ids]
    holdout = [pair for pair in unique.values() if pair['id'] in held_out]
    if set(training_ids) & held_out or len(train) + len(holdout) != len(unique):
        raise ValueError('Original pair identities do not define a disjoint complete split')
    destination = OUT / 'unlabeled-consistency-deduplicated'
    audit = {'created_at': stamp(), 'original_pair_entries': len(source['pairs']),
             'unique_recordings': len(unique), 'duplicate_entries': duplicated,
             'training_recordings': len(train), 'holdout_recordings': len(holdout),
             'training_ids': [pair['id'] for pair in train],
             'holdout_ids': [pair['id'] for pair in holdout],
             'original_pair_sha256': digest(CONSISTENCY / 'pairs.json'),
             'policy': 'Keep original split membership and held-out recordings; remove repeated audio hashes within training. Original reports remain preserved.'}
    save(destination / 'pair-audit.json', audit)
    with threadpool_limits(limits=1):
        for dataset in ('soundtracks-set2', 'cal500', 'gold-vibes'):
            compare(dataset, train, holdout, output_dir=destination)
    save(destination / 'status.json', {'state': 'complete', 'updated_at': stamp(), **audit})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-for-suite', action='store_true')
    parser.add_argument('--pairs', type=int, default=128)
    parser.add_argument('--holdout-pairs', type=int, default=32)
    parser.add_argument('--collect-only', action='store_true')
    args = parser.parse_args()
    CONSISTENCY.mkdir(parents=True, exist_ok=True)
    try:
        if args.wait_for_suite:
            save(CONSISTENCY / 'status.json', {'state': 'waiting_for_native_suite', 'updated_at': stamp()})
            wait_for_suite()
        pairs = collect_pairs(args.pairs + args.holdout_pairs)
        if len(pairs) < args.pairs + args.holdout_pairs:
            raise ValueError('Fewer usable augmentation pairs than requested; keep training and holdout sizes explicit')
        training_pairs, holdout_pairs = pairs[:args.pairs], pairs[args.pairs:]
        if {pair['audio_sha256'] for pair in training_pairs} & {pair['audio_sha256'] for pair in holdout_pairs}:
            raise ValueError('Duplicate audio across unlabeled training and holdout pairs')
        save(CONSISTENCY / 'pair-split.json', {'seed': SEED,
             'training_ids': [pair['id'] for pair in training_pairs],
             'holdout_ids': [pair['id'] for pair in holdout_pairs],
             'policy': 'Seeded content-hash order; holdout pairs never enter fitting or parameter selection.'})
        if not args.collect_only:
            with threadpool_limits(limits=1):
                for dataset in ['soundtracks-set2', 'cal500', 'gold-vibes']:
                    compare(dataset, training_pairs, holdout_pairs)
        save(CONSISTENCY / 'status.json', {'state': 'complete', 'training_pairs': len(training_pairs),
             'holdout_pairs': len(holdout_pairs), 'updated_at': stamp()})
    except Exception as error:
        save(CONSISTENCY / 'status.json', {'state': 'failed', 'error': repr(error), 'updated_at': stamp()})
        raise


if __name__ == '__main__':
    main()
