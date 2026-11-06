"""Hash-bound diagnostic of rankings within each fitted heldout model.

Pooled out-of-fold rankings combine scores from models never served together.
This supplementary analysis preserves the original failed protocol. It cannot
select a candidate or establish prospective qualification from reused results.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

import numpy as np

from . import vibe_pooling_evaluation as evaluation

FORMAT = 'within-model-pooling-ranking-diagnostic-1'
CODE = ('experiments/audio_lab_tools/vibe_pooling_ranking.py',
        'experiments/audio_lab_tools/vibe_pooling_evaluation.py')
RATIONALE = ('Evaluate recommendation order only among targets scored by the same fitted model. '
    'Pooling out-of-fold scores introduces cross-model calibration differences into a list '
    'that no product model serves. Preserve the old pooled results; this is posthoc diagnosis, '
    'not a new prospective adoption result or permission to change qualification.')


def identity():
    return {name: evaluation.digest((evaluation.ROOT / name).read_bytes()) for name in CODE}


def weighted_ndcg(rows, field, weights):
    def dcg(values):
        result, position = 0., 0
        for row in values:
            repeats = min(10 - position, weights[row['cluster']])
            for _ in range(repeats):
                result += (2 ** row['truth'] - 1) / math.log2(position + 2)
                position += 1
            if position == 10:
                break
        return result
    ideal = dcg(sorted(rows, key=lambda row: (-row['truth'], row['recording'])))
    actual = dcg(sorted(rows, key=lambda row: (-row[field], row['recording'])))
    return actual / ideal if ideal else 1.


def within_model_metrics(tasks, weights=None):
    """Count each recording once, ranking separately inside its fitted fold."""
    if not tasks:
        raise ValueError('No fitted-model tasks')
    weights = (Counter({row['cluster']: 1 for task in tasks for row in task['rows']})
               if weights is None else weights)
    totals = Counter()
    seen, folds = set(), set()
    for task in tasks:
        if task['outcome'] != 'complete' or task['fold'] in folds:
            raise ValueError('Incomplete or duplicate fitted-model task')
        folds.add(task['fold'])
        rows = task['rows']
        for row in rows:
            if row['recording'] in seen or row['interval90'] is None:
                raise ValueError('Duplicate target or missing predictive interval')
            seen.add(row['recording'])
        count = sum(weights[row['cluster']] for row in rows)
        if not count:
            continue
        totals['recordings'] += count
        for field in ('baseline', 'candidate'):
            totals[field + '_mae'] += sum(weights[row['cluster']]
                * abs(row['truth'] - row[field]) for row in rows)
            totals[field + '_ndcg10'] += count * weighted_ndcg(rows, field, weights)
        totals['coverage90'] += sum(weights[row['cluster']]
            * (row['interval90'][0] <= row['truth'] <= row['interval90'][1]) for row in rows)
        totals['interval_width'] += sum(weights[row['cluster']]
            * (row['interval90'][1] - row['interval90'][0]) for row in rows)
    count = totals.pop('recordings', 0)
    if not count:
        return None
    return {'recordings': count, **{field: value / count for field, value in totals.items()}}


def freeze(campaign, destination):
    plan = campaign / 'plan.json'
    candidate = json.loads(plan.read_text())
    tasks = []
    for stratum in ('known', 'unseen'):
        for fold in range(evaluation.CRITERIA['folds']):
            for vibe in evaluation.home_vibe.PRESETS:
                path = campaign / f'{stratum}-{fold}-{vibe}.json'
                report = json.loads(path.read_text())
                if (report['plan_sha256'] != evaluation.digest(plan.read_bytes())
                        or report['outcome'] != 'complete'
                        or (report['stratum'], report['fold'], report['vibe']) != (stratum, fold, vibe)):
                    raise ValueError('Incomplete or mismatched campaign')
                tasks.append({'path': str(path.resolve()), 'sha256': evaluation.digest(path.read_bytes())})
    value = {'format': FORMAT, 'code': identity(), 'rationale': RATIONALE,
        'campaign_plan_sha256': evaluation.digest(plan.read_bytes()), 'candidate': candidate['candidate'],
        'criteria': evaluation.CRITERIA, 'tasks': tasks, 'bootstrap_seed': 819,
        'fold_aggregation': 'target-count-weighted mean of NDCG@10 within fitted folds',
        'posthoc_diagnostic': True, 'candidate_selected': False}
    evaluation.save_new(destination, value)
    return {'sha256': evaluation.digest(destination.read_bytes()), 'tasks': len(tasks)}


def run(protocol, output):
    plan = json.loads(protocol.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity()
            or plan['criteria'] != evaluation.CRITERIA or plan['rationale'] != RATIONALE):
        raise ValueError('Diagnostic configuration changed; freeze a separate protocol')
    grouped, task_ids = defaultdict(list), set()
    for item in plan['tasks']:
        path = Path(item['path'])
        if evaluation.digest(path.read_bytes()) != item['sha256']:
            raise ValueError('Frozen task changed')
        task = json.loads(path.read_text())
        key = task['stratum'], task['fold'], task['vibe']
        if key in task_ids or task['plan_sha256'] != plan['campaign_plan_sha256']:
            raise ValueError('Duplicate or mismatched task')
        task_ids.add(key)
        grouped[task['stratum'], task['vibe']].append(task)
    expected = {(s, f, v) for s in ('known', 'unseen') for f in range(evaluation.CRITERIA['folds'])
                for v in evaluation.home_vibe.PRESETS}
    if task_ids != expected:
        raise ValueError('Incomplete fitted-model coverage')
    clusters = sorted({row['cluster'] for tasks in grouped.values() for task in tasks for row in task['rows']})
    measures = {key: within_model_metrics(tasks) for key, tasks in grouped.items()}
    def gain(values):
        return float(np.mean([sum(values[s, v]['recordings'] *
            (values[s, v]['baseline_mae'] - values[s, v]['candidate_mae']) for s in ('known', 'unseen'))
            / sum(values[s, v]['recordings'] for s in ('known', 'unseen')) for v in evaluation.home_vibe.PRESETS]))
    rng, intervals, gains = np.random.default_rng(plan['bootstrap_seed']), defaultdict(lambda: defaultdict(list)), []
    for _ in range(evaluation.CRITERIA['bootstrap_repetitions']):
        weights = Counter(rng.choice(clusters, len(clusters), replace=True))
        sampled = {key: within_model_metrics(tasks, weights) for key, tasks in grouped.items()}
        if any(value is None for value in sampled.values()):
            raise ValueError('Bootstrap lacks a required stratum')
        gains.append(gain(sampled))
        for key, value in sampled.items():
            for field, amount in value.items():
                if field != 'recordings':
                    intervals[key][field].append(amount)
    result = {'protocol_sha256': evaluation.digest(protocol.read_bytes()), 'rationale': RATIONALE,
        'posthoc_diagnostic': True, 'candidate_selected': False, 'qualified': False,
        'bootstrap_repetitions': len(gains), 'bootstrap_seed': plan['bootstrap_seed'],
        'macro_absolute_mae_gain': gain(measures),
        'macro_gain_interval95': np.quantile(gains, [.025, .975]).tolist(),
        'metrics': {s: {v: {**measures[s, v], 'interval95': {
            field: np.quantile(values, [.025, .975]).tolist()
            for field, values in intervals[s, v].items()}} for v in evaluation.home_vibe.PRESETS}
            for s in ('known', 'unseen')},
        'limits': ['Original frozen pooled-ranking failures remain unchanged.',
            'Correcting the ranking estimand after observing failures is posthoc development.',
            'Prospective model selection, independent quality and operational acceptance remain unestablished.']}
    evaluation.save_new(output, result)
    return {key: value for key, value in result.items() if key != 'metrics'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    freeze_action = actions.add_parser('freeze')
    freeze_action.add_argument('campaign', type=Path)
    freeze_action.add_argument('destination', type=Path)
    run_action = actions.add_parser('run')
    run_action.add_argument('protocol', type=Path)
    run_action.add_argument('output', type=Path)
    args = parser.parse_args()
    result = freeze(args.campaign, args.destination) if args.action == 'freeze' else run(args.protocol, args.output)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
