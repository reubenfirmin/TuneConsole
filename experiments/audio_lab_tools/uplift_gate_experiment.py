"""Freeze, fit and evaluate the bounded #712 Uplift context experiment."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import resource
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import individual_protocol as protocol, quality_round as evidence
from . import quality_baselines, uplift_gate as models, uplift_consensus as native
from .external_experiment import LAB, digest, save, stamp
from .focus_experiment import arrays, check_receipt, inside, load
from .individual_experiment import compact
from .relax_experiment import global_groups
from .uplift_experiment import OMISSIONS, PRIMARY, add_controls

OLD = LAB / 'local/uplift-618-20261030/round-1'
DEFAULT = LAB / 'local/uplift-gate-712-20261101/round-2'
DOCUMENT = LAB / 'UPLIFT_GATE_PLAN.md'


def freeze(directory):
    inputs, rows, boundaries = {}, [], {}
    original = {}
    for source in native.SOURCES:
        for suffix in ('json', 'npz'):
            inputs[source + '.' + suffix] = OLD / f'inputs/{source}.{suffix}'
        meta = load(inputs[source + '.json'])['rows']
        boundaries[source] = slice(len(rows), len(rows) + len(meta))
        rows.extend(meta)
        original[source] = arrays(inputs[source + '.npz'])
    for path in sorted((LAB / 'local/per-vibe-20261029/all-sources-1/inputs').glob('*.json')):
        inputs['identities/' + path.name] = path
        rows.extend(load(path).get('rows', []))
    local = LAB / 'local/individual-20261030/local-1/labels.json'
    inputs['local-identities.json'] = local
    local_start = len(rows)
    rows.extend(load(local)['rows'])
    groups = global_groups(rows)
    protected = set(groups[local_start:])
    joins = defaultdict(set)
    for source, d in original.items():
        for new, old in zip(groups[boundaries[source]], d['groups'], strict=True):
            joins[int(new)].add(int(old))
        if set(groups[boundaries[source]]) & protected:
            raise ValueError('Prior cohort intersects local protected components')
    inputs['adopted.json'] = LAB.parent.parent / 'src/yt_playlist/rec/data/uplift-vibes.json'
    inputs['frozen-constant.json'] = OLD / 'models/full.json'
    constant = load(inputs['frozen-constant.json'])
    if dict(zip(constant['expert_names'], constant['learned_weights'])) != {
            s: models.CONSTANT.get(s, 0.) for s in native.EXPERTS}:
        raise ValueError('Previously frozen constant weights changed')
    plan = evidence.freeze(directory, 712, {'document_sha256': digest(DOCUMENT),
        'grid': models.GRID, 'penalty': models.PENALTY, 'bootstrap_draws': 1000,
        'minimum_contrast_rows': 15, 'minimum_contrast_groups': 10,
        'acceptance': {'ndcg_gain': .02, 'lift_gain': .01, 'ranking_loss': .01, 'mse_loss': .005},
        'budgets': {'seconds': 7200, 'rss_bytes': 1024 ** 3, 'artifact_bytes': 524288, 'warm_ms': 100},
        'head_policy': 'Same #618 native targets and fitting procedure, refitted for complete identity exclusions; arousal context uses DEAM/PMEmo native arousal.'},
        inputs, references=[OLD / 'plan.json', OLD / 'evaluation.receipt.json',
                            LAB / 'local/uplift-gate-712-20261101/round-1/plan.json'],
        documents=[DOCUMENT, Path(__file__).with_name('test_uplift_gate.py')])
    prepared = {}
    for source, d in original.items():
        d['groups'] = groups[boundaries[source]]
        meta = load(inputs[source + '.json'])
        if source in (native.DEAM, native.PMEMO):
            d['arousal'] = d['native'][:, meta['target_names'].index('arousal')]
        prepared[source] = d
    assignment = native.split(prepared, count=5)
    for source, d in prepared.items():
        d['outer'] = assignment[source]
        path = directory / f'inputs/{source}.npz'
        np.savez_compressed(path, **d)
        plan['inputs'][str(path.relative_to(directory))] = digest(path)
    audit = {'rows': {s: len(d['y']) for s, d in prepared.items()},
             'groups': {s: len(set(d['groups'])) for s, d in prepared.items()},
             'merged_old_components': {str(k): sorted(v) for k, v in joins.items() if len(v) > 1},
             'local_overlap': 0, 'inventory_rows': len(rows),
             'prior_exposure': 'Exact adopted/prior DEAM controls have prior fitting/evaluation exposure. Repaired fixed/anchor/constant and gate are newly cross-fitted.'}
    save(directory / 'inputs/identity-audit.json', audit)
    plan['inputs']['inputs/identity-audit.json'] = digest(directory / 'inputs/identity-audit.json')
    save(directory / 'plan.json', plan)
    evidence.validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'), 'audit': audit}), flush=True)


def datasets(directory):
    return {s: arrays(directory / f'inputs/{s}.npz') for s in native.SOURCES}


def fit_stage(directory, stage, training, validation):
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    started = time.monotonic()
    print(json.dumps({'fit': stage, 'rows': {s: len(d['y']) for s, d in training.items()}}), flush=True)
    model, trace = models.fit(training)
    fitted = set(np.concatenate([d['groups'] for d in training.values()]))
    result = {}
    for source, d in validation.items():
        if fitted & set(d['groups']):
            raise ValueError('Evaluation component occurs in fitting')
        result[source + '__ids'] = d['ids']
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            if key not in d:
                continue
            for name, scores in models.predict(model, d[key], d['counts']).items():
                result[f'{source}__{prep}__{name}'] = scores
            if source in model['experts']:
                result[f'{source}__{prep}__native'] = native.native_average(
                    model['experts'][source]['heads'], d[key], d['counts'])
    compact(directory / f'models/{stage}.json', model)
    save(directory / (stage + '.trace.json'), trace)
    np.savez_compressed(directory / (stage + '.npz'), **result)
    save(directory / (stage + '.resources.json'), {'seconds': time.monotonic() - started,
         'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024})
    evidence.seal(directory, stage, [directory / f'models/{stage}.json',
        *[directory / (stage + suffix) for suffix in ('.trace.json', '.npz', '.resources.json')]])
    print(json.dumps({'completed': stage, 'parameter': model['parameter'], 'seconds': time.monotonic() - started}), flush=True)


def fit_all(directory):
    data = datasets(directory)
    for fold in range(5):
        fit_stage(directory, 'outer-' + str(fold),
            {s: native.subset(d, d['outer'] != fold) for s, d in data.items()},
            {s: native.subset(d, d['outer'] == fold) for s, d in data.items()})
    for name, omitted in OMISSIONS.items():
        protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
        training = {s: native.subset(d, np.array([g not in protected for g in d['groups']]))
                    for s, d in data.items() if s not in omitted}
        fit_stage(directory, 'transfer-' + name, training, {s: data[s] for s in omitted})
    fit_stage(directory, 'full', data, {})


def contrast_report(meta, d, scores):
    names, values = meta['target_names'], d['native']
    get = lambda label: values[:, names.index(label)]
    if meta['dataset'] == native.SOUND:
        masks = {'gentle_positive': (get('energy') <= .35) & (get('happy') >= .65),
                 'energetic_negative': (get('energy') >= .65) & (get('valence') <= .35)}
    elif 'valence' in names and 'arousal' in names:
        masks = {'gentle_positive_proxy': (get('arousal') <= .35) & (get('valence') >= .65),
                 'energetic_negative': (get('arousal') >= .65) & (get('valence') <= .35)}
    else:
        return {}
    result = {}
    for name, mask in masks.items():
        supported = mask.sum() >= 15 and len(set(d['groups'][mask])) >= 10
        result[name] = {'rows': int(mask.sum()), 'groups': len(set(d['groups'][mask])),
            'supported': bool(supported), 'ids': d['ids'][mask].tolist(),
            'reference_mean': float(d['reference'][mask].mean()) if mask.any() else None,
            'metrics': {k: protocol.metrics(d['reference'][mask], v[mask], np.zeros(mask.sum(), dtype=int))
                        for k, v in scores.items()} if supported else {}}
    return result


def table(d, scores, draws):
    comparisons = [('gate', k) for k in scores if k != 'gate']
    return {'rows': len(d['y']), 'groups': len(set(d['groups'])),
            'metrics': {k: protocol.metrics(d['reference'], v, d['outer']) for k, v in scores.items()},
            'paired': protocol.paired(d['reference'], scores, d['outer'], d['groups'], comparisons, draws)}


def decision(report):
    reasons = []
    for prep in ('historical', 'browser'):
        for control in ('fixed', 'constant'):
            cells = [report['outer'][s][prep]['paired']['gate__vs__' + control] for s in PRIMARY]
            gain = np.average([c['ndcg']['difference'] for c in cells], weights=[1, .5, .5, 1])
            lift = np.average([c['top_lift']['difference'] for c in cells], weights=[1, .5, .5, 1])
            if gain < .02 or lift < .01:
                reasons.append(f'{prep} macro gain vs {control}: NDCG {gain:.6f}, lift {lift:.6f}.')
            if not any(c['ndcg']['conditional_95_percent_interval'][0] > 0 for c in cells):
                reasons.append(f'No positive paired primary lower bound: {prep}/{control}.')
    for regime in ('outer', 'transfer'):
        for source in PRIMARY:
            for prep, cell in report[regime][source].items():
                for control in ('fixed', 'constant', 'frozen_constant', 'anchor', 'prior_deam', 'adopted'):
                    pair = cell['paired'].get('gate__vs__' + control)
                    if pair and (pair['ndcg']['difference'] < -.01 or pair['top_lift']['difference'] < -.01
                                 or pair['mse']['difference'] > .005):
                        reasons.append(f'Domain ceiling: {regime}/{source}/{prep}/{control}.')
    gentle = [c for table in report['contrasts'].values() for prep in table.values()
              for name, c in prep.items() if name.startswith('gentle_positive') and c['supported']]
    if not any(c['metrics']['gate']['ndcg'] - c['metrics']['fixed']['ndcg'] >= .02
               and c['metrics']['gate']['top_lift'] - c['metrics']['fixed']['top_lift'] >= .01 for c in gentle):
        reasons.append('No supported gentle-positive slice has the required ranking and lift gain.')
    if not report['resources']['pass']:
        reasons.append('Resource or independent replay limit failed.')
    return {'recommendation': 'retain' if reasons else 'candidate_for_review', 'reasons': reasons}


def evaluate(directory, plan):
    started = time.monotonic()
    data, saved = datasets(directory), {}
    adopted = load(directory / 'inputs/adopted.json')
    frozen_constant = load(directory / 'inputs/frozen-constant.json')
    report = {'created_at': stamp(), 'outer': {}, 'transfer': {}, 'native': {}, 'contrasts': {}}
    for regime in ('outer', 'transfer'):
        for source, original in data.items():
            if regime == 'transfer' and source not in PRIMARY:
                continue
            keep = original['common']
            d = native.subset(original, keep)
            report[regime][source] = {}
            for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
                if key not in d:
                    continue
                scores = {k: np.empty(len(original['y'])) for k in ('gate', 'fixed', 'constant', 'anchor')}
                predicted_native = np.empty_like(original['y'])
                stages = [('outer-' + str(f), original['outer'] == f) for f in range(5)] if regime == 'outer' else [
                    ('transfer-' + next(k for k, v in OMISSIONS.items() if source in v), np.ones(len(original['y']), dtype=bool))]
                for stage, mask in stages:
                    check_receipt(directory, stage + '.receipt.json')
                    value = arrays(directory / (stage + '.npz'))
                    if not np.array_equal(value[source + '__ids'], original['ids'][mask]):
                        raise ValueError('Saved prediction identities differ')
                    for method in scores:
                        scores[method][mask] = value[f'{source}__{prep}__{method}']
                    if regime == 'outer':
                        predicted_native[mask] = value[f'{source}__{prep}__native']
                scores = {k: v[keep] for k, v in scores.items()}
                scores['adopted'] = quality_baselines.predict(adopted, d[key], d['counts'])[0]
                scores['frozen_constant'] = native.predict(frozen_constant, d[key], d['counts'])['learned']
                controls = {}
                add_controls(d, controls, prep)
                if 'prior_deam' in controls:
                    scores['prior_deam'] = controls['prior_deam']
                print(json.dumps({'evaluate': regime, 'source': source, 'preparation': prep}), flush=True)
                report[regime][source][prep] = table(d, scores, plan['protocol']['bootstrap_draws'])
                if regime == 'outer':
                    report['native'].setdefault(source, {})[prep] = {
                        label: {'mse': float(np.mean((predicted_native[keep, i] - d['y'][:, i]) ** 2)),
                                'mae': float(np.mean(np.abs(predicted_native[keep, i] - d['y'][:, i])))}
                        for i, label in enumerate(native.NATIVE[source])}
                    report['contrasts'].setdefault(source, {})[prep] = contrast_report(
                        load(directory / f'inputs/{source}.json'), d, scores)
                saved.update({f'{regime}__{source}__{prep}__{k}': v for k, v in scores.items()})
            saved['ids__' + source] = d['ids']
    model = load(directory / 'models/full.json')
    chunks = data[native.MEMO]['chunks'][:3]
    counts = data[native.MEMO]['counts'][:3]
    first = models.predict(model, chunks, counts)
    independent = models.predict(model, chunks, counts, True)
    replay_error = max(float(np.max(np.abs(first[k] - independent[k]))) for k in first)
    elapsed = []
    for _ in range(101):
        start = time.perf_counter()
        models.predict(model, chunks[:1, :3], np.array([3]))
        elapsed.append(1000 * (time.perf_counter() - start))
    fit_seconds = sum(load(p)['seconds'] for p in directory.glob('*.resources.json'))
    rss = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              *[load(p)['peak_rss_bytes'] for p in directory.glob('*.resources.json')])
    size = (directory / 'models/full.json').stat().st_size
    report['resources'] = {'artifact_bytes': size, 'fit_seconds': fit_seconds,
        'evaluation_seconds': time.monotonic() - started, 'peak_rss_bytes': rss,
        'warm_three_max_ms': max(elapsed[1:]), 'warm_three_median_ms': float(np.median(elapsed[1:])),
        'replay_error': replay_error, 'pass': bool(size <= 524288 and rss <= 1024 ** 3
            and fit_seconds + time.monotonic() - started <= 7200 and max(elapsed[1:]) <= 100 and replay_error <= 1e-12)}
    report['parameters'] = {p.stem: load(p)['parameter'] for p in (directory / 'models').glob('*.json')}
    report['decision'] = decision(report)
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    evidence.seal(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz'])
    print(json.dumps({'decision': report['decision'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'fit', 'evaluate', 'run', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
            return
        plan = evidence.validate(directory, args.action != 'verify')
        if args.action in ('fit', 'run'):
            fit_all(directory)
        if args.action in ('evaluate', 'run'):
            evaluate(directory, plan)
        if args.action == 'verify':
            print(json.dumps(evidence.verify(directory)))


if __name__ == '__main__':
    main()
