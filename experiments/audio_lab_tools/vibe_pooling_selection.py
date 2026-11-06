"""Prospective within-model development check after the ranking correction.

The original failed campaigns remain immutable. A salted, deterministic fold
assignment is frozen before new fits. Reused originals are development evidence,
and passing this check never activates or qualifies a production model.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

import numpy as np

from . import vibe_pooling_evaluation as evaluation, vibe_pooling_metadata as metadata
from . import vibe_pooling_ranking as ranking, vibe_pooling_transfer as transfer

FORMAT = 'within-model-pooling-selection-1'
CODE = ('experiments/audio_lab_tools/vibe_pooling_selection.py',
        'experiments/audio_lab_tools/vibe_pooling_ranking.py',
        'experiments/audio_lab_tools/vibe_pooling_transfer.py')
SALT = '819-within-model-1'
POLICY = {'format': FORMAT, 'fold_salt': SALT,
    'ranking': 'target-count-weighted NDCG@10 computed separately within each fitted fold',
    'candidate': 'learned scalar masked-metadata covariate with separate source residuals',
    'rationale': 'Compare ordering only among targets scored by one fitted model; '
        'pooled out-of-fold calibration is not a served ranking. Preserve all original failed results.',
    'limitations': ['The originals have already informed model development.',
        'New folds and a prospective metric do not create an independent measurement dataset.',
        'No reserved human outcome is used; operational and independent review remain required.']}


def identity():
    return {name: evaluation.digest((evaluation.ROOT / name).read_bytes()) for name in CODE}


def fold_assignment(assignments):
    return {key: {'cluster': row['cluster'],
        'known': int(evaluation.digest(f'{SALT}/recording/{key}'.encode()), 16) % 5,
        'unseen': int(evaluation.digest(f"{SALT}/{row['cluster']}".encode()), 16) % 5}
        for key, row in assignments.items()}


def freeze(source, candidate, output):
    _humans, original = transfer.inputs(source, candidate, 'metadata')
    plan = {**original, 'code': metadata.identity(),
        'assignments': fold_assignment(original['assignments']),
        'selection': POLICY, 'selection_code': identity(),
        'previous_development_plan_sha256': evaluation.digest(candidate.read_bytes())}
    evaluation.save_new(output, plan)
    return {'plan_sha256': evaluation.digest(output.read_bytes()), 'selection': POLICY}


def load(path):
    plan = metadata.load(path)
    if (plan.get('selection') != POLICY or plan.get('selection_code') != identity()
            or plan['assignments'] != fold_assignment(plan['assignments'])):
        raise ValueError('Prospective selection protocol changed; freeze a separate plan')
    return plan


def expected_targets(plan, stratum, fold, vibe):
    hidden = {int(key) for key, value in plan['assignments'].items() if value[stratum] == fold}
    rows = plan['rows'][vibe]
    artists = {artist for row in rows if row['recording'] not in hidden for artist in row['artists']}
    return {row['recording']: row for row in rows if row['recording'] in hidden and row['artists']
            and (stratum == 'unseen' or artists.intersection(row['artists']))}


def summarize(path, results, output, *, plan_loader=load):
    plan = plan_loader(path)
    plan_hash = evaluation.digest(path.read_bytes())
    tasks, grouped = {}, defaultdict(list)
    for source in results:
        report = json.loads(source.read_text())
        key = report['stratum'], report['fold'], report['vibe']
        if report['plan_sha256'] != plan_hash or key in tasks:
            raise ValueError('Changed or duplicate selection task')
        tasks[key] = report
        if report['outcome'] != 'complete':
            continue
        expected = expected_targets(plan, *key)
        found = {row['recording']: row for row in report['rows']}
        if len(found) != len(report['rows']) or found.keys() != expected.keys():
            raise ValueError('Selection targets missing or duplicated')
        for recording, row in found.items():
            original = expected[recording]
            if (row['truth'] != original['score'] or row['source'] != original['source']
                    or row['cluster'] != plan['assignments'][str(recording)]['cluster']):
                raise ValueError('Selection target identity changed')
            if (any(not math.isfinite(row[field]) or not 0 <= row[field] <= 1
                    for field in ('truth', 'baseline', 'candidate'))
                    or row['interval90'] is None or len(row['interval90']) != 2
                    or not 0 <= row['interval90'][0] <= row['interval90'][1] <= 1):
                raise ValueError('Selection target lacks a valid score or predictive interval')
        grouped[key[0], key[2]].append(report)
    expected = {(s, f, v) for s in ('known', 'unseen') for f in range(5)
                for v in evaluation.home_vibe.PRESETS}
    complete = tasks.keys() == expected and all(row['outcome'] == 'complete' for row in tasks.values())
    result = {'plan_sha256': plan_hash, 'complete': complete,
        'development_only': True, 'candidate_selected': False, 'statistical_qualification': False,
        'selection': plan['selection'], 'failed_gates': [], 'metrics': {}}
    if not complete:
        result['failed_gates'].append('incomplete campaign')
        evaluation.save_new(output, result)
        return result
    cells = list(grouped)
    metrics = {key: ranking.within_model_metrics(grouped[key]) for key in cells}

    def macro_gain(values):
        ratios, absolute = [], []
        for vibe in evaluation.home_vibe.PRESETS:
            count = sum(values[s, vibe]['recordings'] for s in ('known', 'unseen'))
            old, new = [sum(values[s, vibe]['recordings'] * values[s, vibe][field]
                       for s in ('known', 'unseen')) / count
                       for field in ('baseline_mae', 'candidate_mae')]
            ratios.append((old, new))
            absolute.append(old - new)
        baseline, candidate = np.mean(ratios, axis=0)
        return float((baseline - candidate) / baseline), float(np.mean(absolute))

    criteria, failures = plan['criteria'], result['failed_gates']
    result['macro_mae_gain'], result['macro_absolute_mae_gain'] = macro_gain(metrics)
    if result['macro_mae_gain'] < criteria['macro_mae_gain']:
        failures.append('macro MAE gain')
    for stratum in ('known', 'unseen'):
        old = np.mean([metrics[stratum, vibe]['baseline_mae'] for vibe in evaluation.home_vibe.PRESETS])
        new = np.mean([metrics[stratum, vibe]['candidate_mae'] for vibe in evaluation.home_vibe.PRESETS])
        if new > old * (1 + criteria['stratum_mae_regression']):
            failures.append([stratum, 'stratum MAE regression'])
    for (stratum, vibe), value in metrics.items():
        rows = [row for task in grouped[stratum, vibe] for row in task['rows']]
        clusters = {row['cluster'] for row in rows}
        if (len(rows) < criteria['minimum_recordings_per_vibe_stratum']
                or len(clusters) < criteria['minimum_artists']):
            failures.append([stratum, vibe, 'adequacy'])
        if value['baseline_ndcg10'] - value['candidate_ndcg10'] > criteria['ndcg10_drop']:
            failures.append([stratum, vibe, 'ranking'])
        if not criteria['coverage90'][0] <= value['coverage90'] <= criteria['coverage90'][1]:
            failures.append([stratum, vibe, 'coverage'])
    if any(task['fit_seconds'] > 10 or task['diagnostics']['accounted_memory_bytes'] > 128 * 1024 * 1024
           for task in tasks.values()):
        failures.append('fit resource bound')
    clusters = sorted({row['cluster'] for reports in grouped.values() for task in reports for row in task['rows']})
    rng, gains = np.random.default_rng(819), []
    for _ in range(criteria['bootstrap_repetitions']):
        weights = Counter(rng.choice(clusters, len(clusters), replace=True))
        sampled = {key: ranking.within_model_metrics(grouped[key], weights) for key in cells}
        if any(value is None for value in sampled.values()):
            raise ValueError('Artist-cluster bootstrap lacks a required cell')
        gains.append(macro_gain(sampled)[1])
    result['macro_gain_interval95'] = np.quantile(gains, [.025, .975]).tolist()
    if result['macro_gain_interval95'][0] <= 0:
        failures.append('paired artist-cluster gain interval')
    result['metrics'] = {s: {v: metrics[s, v] for v in evaluation.home_vibe.PRESETS}
                         for s in ('known', 'unseen')}
    result['development_gates_pass'] = not failures
    evaluation.save_new(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'summarize'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=evaluation.home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None or args.candidate is None:
            parser.error('freeze requires --source and --candidate')
        result = freeze(args.source, args.candidate, args.plan)
    else:
        if args.output is None:
            parser.error(args.action + ' requires --output')
        load(args.plan)
        if args.action == 'run':
            if args.stratum is None or args.fold is None or args.vibe is None:
                parser.error('run requires --stratum, --fold and --vibe')
            result = metadata.run(args.plan, args.output, args.stratum, args.fold, args.vibe)
        else:
            result = summarize(args.plan, args.result, args.output)
    print(json.dumps(result, indent=2, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
