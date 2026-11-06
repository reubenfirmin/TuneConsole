"""Categorical human transfer for a separately frozen descriptor candidate.

The measurement campaign and the typed human inventory retain their original
bytes. This protocol binds both, and requires a prospective seal before any
reserved predictions. Positive labels are never numeric fitting observations.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import time

from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.rec import home_vibe, vibe_joint_model as joint, vibe_prediction as baseline
from . import vibe_pooling_descriptors as descriptors, vibe_pooling_evaluation as evaluation
from . import vibe_pooling_labels as labels, vibe_pooling_metadata as metadata

FORMAT = 'descriptor-categorical-transfer-1'
CODE_FILE = 'experiments/audio_lab_tools/vibe_pooling_transfer.py'


def identity():
    return {**descriptors.identity(), CODE_FILE: evaluation.digest(Path(__file__).read_bytes())}


def freeze(source, candidate, destination):
    humans, fitted = labels.load(source), descriptors.load(candidate)
    if fitted['source_sha256'] != evaluation.digest(source.read_bytes()):
        raise ValueError('Candidate and labels come from different source captures')
    if {row['recording'] for row in humans['human_targets']} != set(fitted['human_recordings_excluded']):
        raise ValueError('Candidate does not exclude every human target')
    plan = {'format': FORMAT, 'code': identity(),
        'source': str(source.resolve()), 'source_sha256': evaluation.digest(source.read_bytes()),
        'candidate': str(candidate.resolve()), 'candidate_sha256': evaluation.digest(candidate.read_bytes()),
        'criteria': evaluation.CRITERIA, 'label_policy': labels.LABEL_POLICY,
        'policy': 'Same frozen artist folds and original-only fits; all human identities excluded; no numeric label truth.'}
    evaluation.save_new(destination, plan)
    return {'plan_sha256': evaluation.digest(destination.read_bytes()), 'code': identity()}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity()
            or plan['criteria'] != evaluation.CRITERIA or plan['label_policy'] != labels.LABEL_POLICY):
        raise ValueError('Human transfer configuration changed; freeze a separate protocol')
    source, candidate = Path(plan['source']), Path(plan['candidate'])
    if (evaluation.digest(source.read_bytes()) != plan['source_sha256']
            or evaluation.digest(candidate.read_bytes()) != plan['candidate_sha256']):
        raise ValueError('Human transfer inputs changed')
    return plan, labels.load(source), descriptors.load(candidate)


def seal(path, output):
    plan, _humans, _fitted = load(path)
    value = {'format': FORMAT + '/seal', 'plan_sha256': evaluation.digest(path.read_bytes()),
        'code': identity(), 'candidate_sha256': plan['candidate_sha256'], 'criteria': plan['criteria'],
        'policy': 'Report all reserved outcomes for this candidate; do not select or retune on them.'}
    evaluation.save_new(output, value)
    return value


def run(path, output, stratum, fold, vibe, partition='development', candidate_seal=None):
    plan, humans, fitted = load(path)
    plan_hash = evaluation.digest(path.read_bytes())
    if partition == 'reserved':
        if candidate_seal is None:
            raise ValueError('Reserved judgments require a prospective candidate seal')
        sealed = json.loads(candidate_seal.read_text())
        if (sealed['format'] != FORMAT + '/seal' or sealed['plan_sha256'] != plan_hash
                or sealed['code'] != identity() or sealed['candidate_sha256'] != plan['candidate_sha256']
                or sealed['criteria'] != plan['criteria']):
            raise ValueError('Reserved candidate differs from its seal')
    allocation = humans['assignments']
    hidden = {int(key) for key, value in allocation.items() if value[stratum] == fold}
    human_ids = {row['recording'] for row in humans['human_targets']}
    training = [{**row, 'metadata_prior': fitted['descriptor_inputs'][str(row['recording'])]['fallback'][vibe],
        'covariates': fitted['descriptor_inputs'][str(row['recording'])]['covariates']}
        for row in fitted['rows'][vibe] if row['recording'] not in hidden | human_ids]
    selected = [row for row in humans['human_targets'] if row['partition'] == partition and row['recording'] in hidden]
    report = {'format': FORMAT, 'plan_sha256': plan_hash, 'code': identity(), 'partition': partition,
        'stratum': stratum, 'fold': fold, 'vibe': vibe, 'training_recordings': len(training), 'rows': [], 'gaps': []}
    started = time.monotonic()
    with descriptors.design_context():
        try:
            with work_budget(10):
                value = metadata.fit(training, vibe)
            report['fit_seconds'] = time.monotonic() - started
            posterior = joint.Posterior(value)
        except WorkBudgetExceeded as exc:
            report.update(outcome='deferred', reason=str(exc))
            evaluation.save_new(output, report)
            return {key: value for key, value in report.items() if key not in ('rows', 'code')}
        artists, stats = {artist for row in training for artist in row['artists']}, {}
        for row in training:
            credit = json.dumps(row['artists'], separators=(',', ':')) if row['artists'] else None
            for group in baseline.groups(credit, row['genre']):
                stats[group] = baseline.adjust(stats.get(group, {}), {'fits': {vibe: row}}, 1)
        for human in selected:
            recording = human['recording']
            row = humans['metadata'][str(recording)]
            known = bool(artists.intersection(row['artists']))
            if not row['artists'] or stratum == 'known' and not known:
                report['gaps'].append({'recording': recording,
                    'reason': 'no artist' if not row['artists'] else 'artist absent from fitting'})
                continue
            if stratum == 'unseen' and known:
                raise ValueError('Artist leaked across a wholly unseen fold')
            profile = evaluation.masked_profile(humans['profile_inputs'][str(recording)])
            if profile is None:
                report['gaps'].append({'recording': recording, 'reason': 'no masked production profile'})
                continue
            fallback = home_vibe.heuristic_affinity(profile, vibe)
            target = {**row, 'metadata_prior': fallback, 'covariates': descriptors.descriptors(profile)}
            score, interval = metadata.predict(posterior, target)
            credit = json.dumps(row['artists'], separators=(',', ':'))
            old = baseline.resolve({vibe: fallback}, None,
                [(key, stats[key]) for key in baseline.groups(credit, row['genre']) if key in stats])['fits'][vibe]['score']
            report['rows'].append({'recording': recording, 'cluster': allocation[str(recording)]['cluster'],
                'labels': human['labels'], 'baseline': old, 'candidate': score, 'interval90': interval})
    report.update(outcome='complete', wall_seconds=time.monotonic() - started, diagnostics=value['diagnostics'])
    evaluation.save_new(output, report)
    return {key: value for key, value in report.items() if key not in ('rows', 'code', 'diagnostics')}


def summarize(path, results, output):
    _plan, humans, _fitted = load(path)
    plan_hash, tasks, records, gaps = evaluation.digest(path.read_bytes()), {}, defaultdict(dict), []
    truth = {row['recording']: row for row in humans['human_targets']}
    for source in results:
        report = json.loads(source.read_text())
        key = report['partition'], report['stratum'], report['fold'], report['vibe']
        if report['plan_sha256'] != plan_hash or report['code'] != identity() or key in tasks:
            raise ValueError('Changed or duplicate human transfer task')
        tasks[key] = report
        gaps.extend({'task': list(key), **gap} for gap in report['gaps'])
        for row in report['rows']:
            target, assignment = truth[row['recording']], humans['assignments'][str(row['recording'])]
            if (target['labels'] != row['labels'] or target['partition'] != key[0]
                    or assignment[key[1]] != key[2] or assignment['cluster'] != row['cluster']):
                raise ValueError('Human target identity changed')
            record = records[key[:2]].setdefault(row['recording'],
                {'recording': row['recording'], 'cluster': row['cluster'], 'labels': row['labels'],
                 'baseline': {}, 'candidate': {}})
            for policy in ('baseline', 'candidate'):
                if key[3] in record[policy]:
                    raise ValueError('Duplicate human target/vibe')
                record[policy][key[3]] = row[policy]
    partitions = sorted({key[0] for key in tasks})
    expected = {(p, s, f, v) for p in partitions for s in ('known', 'unseen')
                for f in range(5) for v in home_vibe.PRESETS}
    metrics = {}
    for partition in partitions:
        metrics[partition] = {}
        for stratum in ('known', 'unseen'):
            complete = []
            for row in records[partition, stratum].values():
                missing = set(home_vibe.PRESETS) - row['candidate'].keys()
                if missing:
                    gaps.append({'partition': partition, 'stratum': stratum, 'recording': row['recording'],
                        'reason': 'incomplete six-vibe prediction', 'missing_vibes': sorted(missing)})
                else:
                    complete.append(row)
            metrics[partition][stratum] = labels.categorical_metrics(complete)
    value = {'format': FORMAT, 'plan_sha256': plan_hash,
        'complete': bool(tasks) and set(tasks) == expected and all(row['outcome'] == 'complete' for row in tasks.values()),
        'metrics': metrics, 'gaps': gaps, 'inventory': labels.inventory(humans),
        'candidate_selected': False, 'statistical_qualification': False}
    evaluation.save_new(output, value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'seal', 'run', 'summarize'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seal', type=Path)
    parser.add_argument('--partition', choices=('development', 'reserved'), default='development')
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None or args.candidate is None:
            parser.error('freeze requires --source and --candidate')
        value = freeze(args.source, args.candidate, args.plan)
    else:
        if args.output is None:
            parser.error(args.action + ' requires --output')
        if args.action == 'seal':
            value = seal(args.plan, args.output)
        elif args.action == 'run':
            if args.stratum is None or args.fold is None or args.vibe is None:
                parser.error('run requires --stratum, --fold and --vibe')
            value = run(args.plan, args.output, args.stratum, args.fold, args.vibe, args.partition, args.seal)
        else:
            value = summarize(args.plan, args.result, args.output)
    print(json.dumps(value, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
