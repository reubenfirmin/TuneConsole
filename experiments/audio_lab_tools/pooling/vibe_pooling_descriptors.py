"""Joint metadata-dimension development; frozen originals, no native access.

Preserve every previous protocol. Learn from the legitimate masked dimensions
instead of compressing them into one per-vibe engineering score. Values and
supports are covariates, never asserted observation precisions. This driver does
not publish product generations or consume the reserved human-label outcomes.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix, hstack

from yt_playlist.rec import home_vibe, musical_character, musical_profiles, vibe_joint_model as joint
from . import vibe_pooling_evaluation as evaluation, vibe_pooling_metadata as metadata

FORMAT = 'descriptor-pooling-development-1'
CODE_FILE = 'experiments/audio_lab_tools/pooling/vibe_pooling_descriptors.py'
RECIPE = {**metadata.RECIPE, 'prior': 'full masked heuristic plus ten masked dimensions and their supports',
    'dimensions': list(musical_profiles.DIMENSIONS),
    'covariates': 'center observed values at .5, missing values at zero; separate support columns',
    'coefficient_variance': joint.BASELINE_VARIANCE,
    'hypothesis': 'Avoid information loss in the single heuristic covariate; preserve genre distinctions without selecting genre-specific fixes.'}


def identity():
    return {**metadata.identity(), CODE_FILE: evaluation.digest(Path(__file__).read_bytes()),
        'src/yt_playlist/rec/musical_character.py': evaluation.digest(
            (evaluation.ROOT / 'src/yt_playlist/rec/musical_character.py').read_bytes())}


def descriptors(profile):
    values = {}
    for name in musical_profiles.DIMENSIONS:
        value, support = musical_character.dimension({'musical': profile}, name)
        values['dimension:' + name] = value - .5 if value is not None else 0.
        values['support:' + name] = support
    return values


def freeze(source, destination):
    plan = metadata.frozen_inputs(source)
    frozen = {}
    for recording, inputs in plan['profile_inputs'].items():
        profile = evaluation.masked_profile(inputs)
        if profile is None:
            raise ValueError('Missing production profile; no hard target may disappear')
        frozen[recording] = {'covariates': descriptors(profile),
            'fallback': {vibe: home_vibe.heuristic_affinity(profile, vibe) for vibe in home_vibe.PRESETS}}
    plan.update(format=FORMAT, code=identity(), recipe=RECIPE, descriptor_inputs=frozen,
                hypothesis=RECIPE['hypothesis'])
    evaluation.save_new(destination, plan)
    return {'sha256': evaluation.digest(destination.read_bytes()), 'recordings': len(frozen), 'recipe': RECIPE}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity() or plan['recipe'] != RECIPE
            or plan['candidate'] != joint.configuration() or plan['criteria'] != evaluation.CRITERIA):
        raise ValueError('Descriptor configuration changed; freeze a separate plan')
    if set(plan['descriptor_inputs']) != set(plan['assignments']):
        raise ValueError('Frozen descriptors do not cover every recording')
    return plan


@contextmanager
def design_context():
    with metadata.design_context():
        original = joint._design
        def design(rows):
            matrix, features, kinds = original(rows)
            names = sorted(rows[0]['covariates'])
            if any(sorted(row['covariates']) != names for row in rows):
                raise ValueError('Descriptor dimensions differ across observations')
            covariates = csr_matrix(np.asarray([[row['covariates'][name] for name in names] for row in rows]))
            return (hstack((matrix, covariates), format='csr'),
                [*features, *(('baseline', name) for name in names)], [*kinds, *('baseline' for _ in names)])
        joint._design = design
        try:
            yield
        finally:
            joint._design = original


def run(path, output, stratum, fold, vibe):
    plan = load(path)
    prepared = [{**row, 'metadata_prior': plan['descriptor_inputs'][str(row['recording'])]['fallback'][vibe],
        'covariates': plan['descriptor_inputs'][str(row['recording'])]['covariates']} for row in plan['rows'][vibe]]
    return metadata.run_prepared(plan, path, output, stratum, fold, vibe, prepared, context=design_context)


def summarize(path, results, output):
    return metadata.summarize_plan(load(path), evaluation.digest(path.read_bytes()), results, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'summarize'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None:
            parser.error('freeze requires --source')
        value = freeze(args.source, args.plan)
    elif args.action == 'run':
        if args.output is None or args.stratum is None or args.fold is None or args.vibe is None:
            parser.error('run requires --output, --stratum, --fold and --vibe')
        value = run(args.plan, args.output, args.stratum, args.fold, args.vibe)
    else:
        if args.output is None:
            parser.error('summarize requires --output')
        value = summarize(args.plan, args.result, args.output)
    print(json.dumps(value, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
