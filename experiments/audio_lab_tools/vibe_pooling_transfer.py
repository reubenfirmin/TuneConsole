"""Categorical human transfer for a separately frozen metadata candidate.

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
from . import vibe_pooling_pairs as pairs
from . import vibe_pooling_offset as offset
from . import vibe_pooling_source_genre as source_genre

FORMAT = 'metadata-categorical-transfer-6'
CODE_FILE = 'experiments/audio_lab_tools/vibe_pooling_transfer.py'
RECIPES = ('metadata', 'descriptors', 'pairs', 'offset', 'source-genre')
FOLD_POLICIES = ('fivefold', 'single-model')


def identity():
    return {**descriptors.identity(), **pairs.identity(), **offset.identity(), **source_genre.identity(),
            CODE_FILE: evaluation.digest(Path(__file__).read_bytes())}


def inputs(source, candidate, recipe):
    """Use immutable captures as data; the new protocol pins its own executor.

    Historical capture code need not match today's SQL/file adapters. Neither
    archived configuration nor Python from the capture is executed. Existing
    measurement reports remain bound to their original source identities.
    """
    humans, fitted = json.loads(source.read_text()), json.loads(candidate.read_text())
    candidate_format = {'metadata': metadata.FORMAT, 'descriptors': descriptors.FORMAT,
                        'pairs': pairs.FORMAT, 'offset': offset.FORMAT,
                        'source-genre': source_genre.FORMAT}.get(recipe)
    if (recipe not in RECIPES or humans['format'] != labels.FORMAT or fitted['format'] != candidate_format
            or humans['label_policy'] != labels.LABEL_POLICY
            or any(value['criteria'] != evaluation.CRITERIA
                   or value['candidate'] != joint.configuration()
                   or value['profile_mask'] != evaluation.PROFILE_MASK for value in (humans, fitted))):
        raise ValueError('Incompatible categorical capture or metadata candidate')
    if fitted['source_sha256'] != evaluation.digest(source.read_bytes()):
        raise ValueError('Candidate and labels come from different source captures')
    if {row['recording'] for row in humans['human_targets']} != set(fitted['human_recordings_excluded']):
        raise ValueError('Candidate does not exclude every human target')
    human_ids = set(fitted['human_recordings_excluded'])
    if any(row['recording'] in human_ids for rows in fitted['rows'].values() for row in rows):
        raise ValueError('Human recording entered candidate fitting data')
    if (set(humans['metadata']) != set(humans['assignments'])
            or set(humans['metadata']) != set(humans['profile_inputs'])
            or set(fitted['rows']) != set(home_vibe.PRESETS)
            or any([{key: value for key, value in row.items() if key != 'replicate'}
                    if recipe == 'pairs' else row for row in fitted['rows'][vibe]]
                   != [row for row in humans['rows'][vibe] if row['recording'] not in human_ids]
                   for vibe in home_vibe.PRESETS)):
        raise ValueError('Candidate must retain every nonhuman original from its capture')
    if recipe == 'pairs':
        for rows in fitted['rows'].values():
            for row in rows:
                replicate = row.get('replicate')
                if replicate is not None and (
                        not row['source'].startswith('audio_measurement/')
                        or replicate['source'] != 'provider_measurement/' + row['version']
                        or replicate['version'] != row['version']
                        or not 0 <= replicate['score'] <= 1 or not 0 < replicate['support'] <= 1):
                    raise ValueError('Paired observation does not match its canonical audio original')
    for capture in (humans, fitted):
        if set(capture['profile_inputs']) != set(capture['assignments']):
            raise ValueError('Every recording needs masked conditioning and a fold assignment')
        for key, profile in capture['profile_inputs'].items():
            if (set(profile['overrides']) - {'genre'}
                    or any(source['scope'] not in {'artist', 'release'} for source in profile['sources'])
                    or any(features.get('audio') for features in profile['content'].values() if features is not None)):
                raise ValueError('Target observations leaked into frozen profile conditioning')
            if (capture['assignments'][key] != humans['assignments'][key]
                    or profile != humans['profile_inputs'][key]):
                raise ValueError('Candidate conditioning or target folds changed')
    return humans, fitted


def freeze(source, candidate, destination, recipe='descriptors', fold_policy='fivefold'):
    inputs(source, candidate, recipe)
    if fold_policy not in FOLD_POLICIES:
        raise ValueError('Unknown human transfer fold policy')
    plan = {'format': FORMAT, 'code': identity(), 'recipe': recipe, 'fold_policy': fold_policy,
        'source': str(source.resolve()), 'source_sha256': evaluation.digest(source.read_bytes()),
        'candidate': str(candidate.resolve()), 'candidate_sha256': evaluation.digest(candidate.read_bytes()),
        'criteria': evaluation.CRITERIA, 'label_policy': labels.LABEL_POLICY,
        'policy': 'Original-only fits; all human identities excluded; no numeric label truth.',
        'single_model_policy': 'Known uses all nonhuman originals. Unseen excludes every connected '
            'human artist component, including both partitions; one fitted model ranks the entire target pool.'}
    evaluation.save_new(destination, plan)
    return {'plan_sha256': evaluation.digest(destination.read_bytes()), 'code': identity()}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity()
            or plan['criteria'] != evaluation.CRITERIA or plan['label_policy'] != labels.LABEL_POLICY
            or plan['fold_policy'] not in FOLD_POLICIES):
        raise ValueError('Human transfer configuration changed; freeze a separate protocol')
    source, candidate = Path(plan['source']), Path(plan['candidate'])
    if (evaluation.digest(source.read_bytes()) != plan['source_sha256']
            or evaluation.digest(candidate.read_bytes()) != plan['candidate_sha256']):
        raise ValueError('Human transfer inputs changed')
    humans, fitted = inputs(source, candidate, plan['recipe'])
    return plan, humans, fitted


def candidate_rows(fitted, vibe, recipe):
    for row in fitted['rows'][vibe]:
        if recipe == 'descriptors':
            descriptor = fitted['descriptor_inputs'][str(row['recording'])]
            yield {**row, 'metadata_prior': descriptor['fallback'][vibe],
                   'covariates': descriptor['covariates']}
        else:
            profile = evaluation.masked_profile(fitted['profile_inputs'][str(row['recording'])])
            if profile is None:
                raise ValueError('Missing masked training profile; do not drop an original')
            yield {**row, 'metadata_prior': home_vibe.heuristic_affinity(profile, vibe)}


def hidden_recordings(humans, fold_policy, stratum, fold):
    allocation = humans['assignments']
    if fold_policy == 'fivefold':
        return {int(key) for key, value in allocation.items() if value[stratum] == fold}
    if fold != 0:
        raise ValueError('Single-model human transfer has only fold zero')
    if stratum == 'known':
        return set()
    clusters = {allocation[str(row['recording'])]['cluster'] for row in humans['human_targets']}
    return {int(key) for key, value in allocation.items() if value['cluster'] in clusters}


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
    hidden = hidden_recordings(humans, plan['fold_policy'], stratum, fold)
    human_ids = {row['recording'] for row in humans['human_targets']}
    training = [row for row in candidate_rows(fitted, vibe, plan['recipe'])
                if row['recording'] not in hidden | human_ids]
    selected = [row for row in humans['human_targets'] if row['partition'] == partition
                and (plan['fold_policy'] == 'single-model' or row['recording'] in hidden)]
    report = {'format': FORMAT, 'plan_sha256': plan_hash, 'code': identity(), 'partition': partition,
        'stratum': stratum, 'fold': fold, 'vibe': vibe, 'training_recordings': len(training), 'rows': [], 'gaps': []}
    started = time.monotonic()
    context = {'descriptors': descriptors.design_context, 'metadata': metadata.design_context,
               'offset': offset.design_context,
               'source-genre': source_genre.design_context,
               'pairs': pairs.design_context}[plan['recipe']]
    fit_model = {'pairs': pairs.fit, 'offset': offset.fit,
                 'source-genre': source_genre.fit}.get(plan['recipe'], metadata.fit)
    predict_model = {'pairs': pairs.predict, 'source-genre': source_genre.predict}.get(plan['recipe'], metadata.predict)
    with context():
        try:
            with work_budget(10):
                value = fit_model(training, vibe)
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
            target = {**row, 'metadata_prior': fallback}
            if plan['recipe'] == 'descriptors':
                target['covariates'] = descriptors.descriptors(profile)
            score, interval = predict_model(posterior, target)
            credit = json.dumps(row['artists'], separators=(',', ':'))
            old = baseline.resolve({vibe: fallback}, None,
                [(key, stats[key]) for key in baseline.groups(credit, row['genre']) if key in stats])['fits'][vibe]['score']
            report['rows'].append({'recording': recording, 'cluster': allocation[str(recording)]['cluster'],
                'labels': human['labels'], 'baseline': old, 'candidate': score, 'interval90': interval})
    report.update(outcome='complete', wall_seconds=time.monotonic() - started, diagnostics=value['diagnostics'])
    evaluation.save_new(output, report)
    return {key: value for key, value in report.items() if key not in ('rows', 'code', 'diagnostics')}


def summarize(path, results, output):
    plan, humans, _fitted = load(path)
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
            expected_fold = assignment[key[1]] if plan['fold_policy'] == 'fivefold' else 0
            if (target['labels'] != row['labels'] or target['partition'] != key[0]
                    or expected_fold != key[2] or assignment['cluster'] != row['cluster']):
                raise ValueError('Human target identity changed')
            record = records[key[:2]].setdefault(row['recording'],
                {'recording': row['recording'], 'cluster': row['cluster'], 'labels': row['labels'],
                 'baseline': {}, 'candidate': {}})
            for policy in ('baseline', 'candidate'):
                if key[3] in record[policy]:
                    raise ValueError('Duplicate human target/vibe')
                record[policy][key[3]] = row[policy]
    partitions = sorted({key[0] for key in tasks})
    folds = range(5) if plan['fold_policy'] == 'fivefold' else range(1)
    expected = {(p, s, f, v) for p in partitions for s in ('known', 'unseen')
                for f in folds for v in home_vibe.PRESETS}
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
    parser.add_argument('--recipe', choices=RECIPES, default='descriptors')
    parser.add_argument('--fold-policy', choices=FOLD_POLICIES, default='fivefold')
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
        value = freeze(args.source, args.candidate, args.plan, args.recipe, args.fold_policy)
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
