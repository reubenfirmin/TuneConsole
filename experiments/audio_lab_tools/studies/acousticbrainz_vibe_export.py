"""Export the selected mapping and replay local refits against later measurements."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.rec import acousticbrainz_vibes as model, audio_vibe_arc, audio_vibe_specialists
from yt_playlist.rec.vibe_recording_rules import version

from ..support.external_experiment import digest, save
from ..training import external_training as ridge


def fit_seed(examples, dataset_sha256, groups):
    x = np.array([[row['inputs'][field] for field in model.FIELDS] for row in examples])
    labels = {vibe: np.array([[row['direct'][vibe]] for row in examples]) for vibe in model.VIBES}
    fits = {vibe: ridge.fit_path(x, y, (ridge.choose_alpha(x, y, groups, [0]),))[0]
            for vibe, y in labels.items()}
    shared = next(iter(fits.values()))
    return {'format': 1, 'fields': model.FIELDS, 'arc_version': audio_vibe_arc.VERSION,
            'specialists': audio_vibe_specialists.identity(), 'dataset_sha256': dataset_sha256,
            'training_recordings': len(examples), 'mean': shared['mean'].tolist(), 'scale': shared['scale'].tolist(),
            'alphas': {name: fit['alpha'] for name, fit in fits.items()},
            'coefficients': {name: [float(fit['intercept'][0]), *fit['weights'][:, 0].tolist()]
                             for name, fit in fits.items()}}


def pairs(examples):
    return [([row['inputs'][field] for field in model.FIELDS],
             {vibe: (row['direct'][vibe], 1.) for vibe in model.VIBES}) for row in examples]


def error(seed, coefficients, examples):
    x = (np.array([row[0] for row in examples]) - seed['mean']) / seed['scale']
    target = np.array([[fits[vibe][0] for vibe in model.VIBES] for _values, fits in examples])
    weights = np.array([coefficients[vibe] for vibe in model.VIBES])
    scores = np.clip(x @ weights[:, 1:].T + weights[:, 0], 0, 1)
    return float(np.abs(target - scores).mean())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('first_run', type=Path)
    parser.add_argument('--capture-times', type=Path, required=True)
    parser.add_argument('--fresh', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    first = args.first_run
    dataset = first / 'dataset.json'
    plan = json.loads((first / 'plan.json').read_text())
    results = json.loads((first / 'comparison.json').read_text())
    dataset_sha = digest(dataset)
    if (plan['measurement_version'] != version() or results['dataset_sha256'] != dataset_sha
            or tuple(plan['inputs']['classifiers']) != model.FIELDS):
        raise ValueError('Frozen paired data differs from the specialist targets or classifier contract')
    examples = json.loads(dataset.read_text())['examples']
    groups = {row['recording_id']: row['group'] for row in results['held_out']}
    times = json.loads(args.capture_times.read_text())
    ordered = sorted(examples, key=lambda row: (times[str(row['recording_id'])], row['recording_id']))
    fresh = json.loads(args.fresh.read_text())['examples']
    with threadpool_limits(limits=1):
        seed = fit_seed(examples, dataset_sha, [groups[row['recording_id']] for row in examples])
        early = ordered[:68]
        replay_seed = fit_seed(early, dataset_sha, [groups[row['recording_id']] for row in early])
        # Choose regularization on 34 later recordings; keep 34 for a separate check.
        tuning, confirmation = ordered[68:102], ordered[102:]
        relative = (10., 30., 100., 300., 1000.)
        tuning_losses = {strength: error(replay_seed, model.adjusted(replay_seed, pairs(tuning[:17]), strength=strength),
                                         pairs(tuning[17:])) for strength in relative}
        strength = min(relative, key=tuning_losses.get)
        local = model.adjusted(replay_seed, pairs(tuning), strength=strength)
        seed['local_regularization'] = strength
        save(args.report, {'dataset_sha256': dataset_sha, 'capture_times_sha256': digest(args.capture_times),
             'fresh_sha256': digest(args.fresh), 'chronological_recordings': [row['recording_id'] for row in ordered],
             'sizes': [len(early), len(tuning), len(confirmation), len(fresh)],
             'tuning_mae': tuning_losses, 'local_regularization': strength,
             'confirmation_mae': {'bundled': error(replay_seed, replay_seed['coefficients'], pairs(confirmation)),
                                  'local': error(replay_seed, local, pairs(confirmation))},
             'fresh_mae': error(seed, seed['coefficients'], pairs(fresh))})
        save(args.output, seed)


if __name__ == '__main__':
    main()
