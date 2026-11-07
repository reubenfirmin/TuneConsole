"""Shared classifier-by-genre dependence, detached development only.

Each canonical original still enters once. A separately estimated variance
accounts for genre-specific offsets shared by one exact measurement source.
Predictions integrate those offsets over the training source/support mixture;
they do not identify an unbiased latent human suitability scale.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path

from yt_playlist.rec import home_vibe, vibe_joint_model as joint
from . import vibe_pooling_evaluation as evaluation, vibe_pooling_metadata as metadata

FORMAT = 'source-genre-pooling-development-1'
CODE_FILE = 'experiments/audio_lab_tools/pooling/vibe_pooling_source_genre.py'
RECIPE = {**metadata.RECIPE,
    'source_genre': 'separate marginal-ML variance; shared by exact source and specific genre',
    'source_genre_initial_variance': .01,
    'gradient': 'forward-relative finite difference, step 1e-5',
    'prediction': 'empirical source/support mixture, including source/genre offsets',
    'hypothesis': 'Shared classifier-by-genre error must not manufacture independent genre evidence.',
    'limitations': 'Source/genre offsets remain partly confounded with genre effects; no source is ground truth.'}


def identity():
    return {**metadata.identity(), CODE_FILE: evaluation.digest(Path(__file__).read_bytes())}


def freeze(source, candidate, destination):
    from . import vibe_pooling_transfer as transfer
    _humans, original = transfer.inputs(source, candidate, 'metadata')
    plan = {**original, 'format': FORMAT, 'recipe': RECIPE, 'code': identity(),
        'hypothesis': RECIPE['hypothesis'],
        'previous_development_plan_sha256': evaluation.digest(candidate.read_bytes())}
    evaluation.save_new(destination, plan)
    return {'plan_sha256': evaluation.digest(destination.read_bytes()), 'recipe': RECIPE}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity() or plan['recipe'] != RECIPE
            or plan['criteria'] != evaluation.CRITERIA or plan['candidate'] != joint.configuration()):
        raise ValueError('Source/genre configuration changed; freeze a separate plan')
    return plan


@contextmanager
def design_context():
    original_components, original_initial = joint.COMPONENTS, joint.INITIAL
    joint.COMPONENTS = (*original_components[:-1], 'source_genre', 'residual')
    joint.INITIAL = {**original_initial, 'source_genre': RECIPE['source_genre_initial_variance']}
    try:
        with metadata.design_context():
            original_terms = joint.terms
            def terms(row, kind):
                if kind == 'source_genre':
                    return [(json.dumps([row['source'], row['genre']]), 1.)] if row['genre'] else []
                return original_terms(row, kind)
            joint.terms = terms
            try:
                yield
            finally:
                joint.terms = original_terms
    finally:
        joint.COMPONENTS, joint.INITIAL = original_components, original_initial


def predict(posterior, row):
    return metadata.predict(posterior, row, measurement_components=('source', 'source_genre'))


def fit(rows, vibe):
    return metadata.fit(rows, vibe, finite_gradient='2-point')


def run(path, output, stratum, fold, vibe):
    plan = load(path)
    prepared = []
    for row in plan['rows'][vibe]:
        profile = evaluation.masked_profile(plan['profile_inputs'][str(row['recording'])])
        if profile is None:
            raise ValueError('Missing masked training profile; do not drop an original')
        prepared.append({**row, 'metadata_prior': home_vibe.heuristic_affinity(profile, vibe)})
    return metadata.run_prepared(plan, path, output, stratum, fold, vibe, prepared,
        context=design_context, fit_model=fit, predict_model=predict)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None or args.candidate is None:
            parser.error('freeze requires --source and --candidate')
        result = freeze(args.source, args.candidate, args.plan)
    else:
        if args.output is None or args.stratum is None or args.fold is None or args.vibe is None:
            parser.error('run requires --output, --stratum, --fold and --vibe')
        result = run(args.plan, args.output, args.stratum, args.fold, args.vibe)
    print(json.dumps(result, indent=2, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
