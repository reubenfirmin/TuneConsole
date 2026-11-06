"""Prospective corrected-ranking evaluation of the integrated runtime model.

Use the same masked originals and folds as the scalar development comparison,
but call the product fitter, posterior and prediction method without patches.
This is reused development evidence; no result activates a configuration.
"""
import argparse
import json
from pathlib import Path

from yt_playlist.rec import home_vibe, vibe_joint_model as joint
from . import vibe_pooling_evaluation as evaluation
from . import vibe_pooling_selection as selection, vibe_pooling_transfer as transfer

FORMAT = 'runtime-within-model-pooling-1'
POLICY = {**selection.POLICY, 'format': FORMAT,
    'candidate': 'unmodified integrated product fitter, posterior and prediction method',
    'rationale': 'Apply the already frozen within-model ranking correction to the runtime '
        'candidate. Keep the same numerical limits and salted folds as the scalar comparison; '
        'preserve the earlier pooled-fold failures and report human outcomes separately.'}


def identity():
    names = (*evaluation.CODE, *selection.CODE,
             'experiments/audio_lab_tools/vibe_pooling_runtime.py')
    return {name: evaluation.digest((evaluation.ROOT / name).read_bytes()) for name in names}


def freeze(source, candidate, output):
    # These historical files supply data only. Their executor is never loaded.
    _humans, original = transfer.inputs(source, candidate, 'metadata')
    plan = {**original, 'format': FORMAT, 'code': identity(), 'selection': POLICY,
        'candidate': joint.configuration(), 'human_targets': [],
        'assignments': selection.fold_assignment(original['assignments']),
        'comparison_plan_sha256': evaluation.digest(candidate.read_bytes())}
    evaluation.save_new(output, plan)
    return {'plan_sha256': evaluation.digest(output.read_bytes()), 'selection': POLICY,
            'human_recordings_excluded': len(plan['human_recordings_excluded'])}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity() or plan['selection'] != POLICY
            or plan['criteria'] != evaluation.CRITERIA or plan['candidate'] != joint.configuration()
            or plan['profile_mask'] != evaluation.PROFILE_MASK or plan['human_targets']
            or plan['assignments'] != selection.fold_assignment(plan['assignments'])
            or set(plan['profile_inputs']) != set(plan['assignments'])
            or set(plan['rows']) != set(home_vibe.PRESETS)):
        raise ValueError('Runtime development protocol changed; freeze a separate plan')
    excluded = set(plan['human_recordings_excluded'])
    for vibe, rows in plan['rows'].items():
        if any(row['recording'] in excluded for row in rows):
            raise ValueError('Human recording entered runtime development fitting data')
        joint.unique(rows)
    for profile in plan['profile_inputs'].values():
        if (set(profile['overrides']) - {'genre'}
                or any(source['scope'] not in {'artist', 'release'} for source in profile['sources'])
                or any(features.get('audio') for features in profile['content'].values() if features is not None)):
            raise ValueError('Recording observations leaked into masked runtime comparison')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'summarize'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None or args.candidate is None:
            parser.error('freeze requires --source and --candidate')
        result = freeze(args.source, args.candidate, args.plan)
    else:
        if args.output is None:
            parser.error(args.action + ' requires --output')
        if args.action == 'run':
            if args.stratum is None or args.fold is None or args.vibe is None:
                parser.error('run requires --stratum, --fold and --vibe')
            result = evaluation.run(args.plan, args.output, args.stratum, args.fold, args.vibe,
                                    plan_loader=load)
        else:
            result = selection.summarize(args.plan, args.result, args.output, plan_loader=load)
    print(json.dumps(result, indent=2, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
