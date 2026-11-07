"""Pinned #576 native Reading/Studying comparison; experiments-only artifacts."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import resource
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies import vibe_specialist_models as learner
from ..support.external_experiment import LAB, digest, save, stamp
from ..training.external_training import folds, validation_groups
from ..studies.mapped_vibe_inference import replay
from ..evaluation.mapped_vibe_metrics import discounted_gain, summary, top_count
from ..evaluation.transfer_identity import exclusion

EXPERIMENTS = LAB.parent
ROOT = LAB / 'local/mapped-vibes-20261029'
DEFAULT = LAB / 'local/individual-20261030/focus-1'
LOCAL = LAB / 'local/individual-20261030/local-1'
BROWSER = LAB / 'local/preprocessing-quality-20261029/run-1'
SOURCE = 'cal500-raw-activity'
TARGETS = ['Usage-Reading', 'Usage-Studying']
SEED = 57630
METHODS = {'historical_embedding': 'embedding', 'historical_combined': 'combined',
           'browser_embedding': 'browser'}


def inside(path):
    path = Path(path).resolve()
    if not path.is_relative_to(EXPERIMENTS):
        raise ValueError('All experiment output must stay under experiments/')
    return path


def load(path):
    return json.loads(Path(path).read_text())


def arrays(path):
    with np.load(path, allow_pickle=False) as value:
        return {key: value[key] for key in value.files}


def runtime():
    return {'python': sys.version, **{name: importlib.metadata.version(name)
            for name in ('numpy', 'scipy', 'threadpoolctl')}}


def source_paths():
    # Archive the actually imported research dependency closure, including the
    # native numerical kernels, not just the top-level command.
    paths = {Path(module.__file__).resolve() for name, module in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(module, '__file__', None)}
    paths.update((Path(__file__).resolve(), EXPERIMENTS / 'run.py',
                  LAB / 'FOCUS_EXPERIMENT_PLAN.md', Path(__file__).with_name('test_focus_experiment.py')))
    return {str(path.relative_to(EXPERIMENTS)): path for path in sorted(paths)}


def check_receipt(directory, filename, expected_plan=True):
    receipt = load(directory / filename)
    plan_identity = receipt.get('plan_sha256', receipt.get('fingerprint'))
    if expected_plan and plan_identity != digest(directory / 'plan.json'):
        raise ValueError('Receipt plan identity differs: ' + str(directory / filename))
    for name, checksum in receipt['outputs'].items():
        if digest(directory / name) != checksum:
            raise ValueError('Receipt output changed: ' + str(directory / name))
    return receipt


def group_guard(groups, split):
    groups, split = np.asarray(groups), np.asarray(split)
    if len(groups) != len(split) or len(set(split)) < 2:
        raise ValueError('Insufficient grouped folds')
    if any(len(set(split[groups == group])) != 1 for group in set(groups)):
        raise ValueError('Connected identity group crosses folds')


def grid():
    return [spec for spec in learner.grid() if spec['family'] in ('ridge', 'rff')]


def native_predict(model, x):
    if model['kind'] == 'constant':
        return np.broadcast_to(model['value'], (len(x), len(model['value']))).copy()
    return learner.predict(model, x)


def constant(y, dimension):
    return {'kind': 'constant', 'value': np.mean(y, axis=0), 'medians': np.zeros(dimension),
            'mean': np.zeros(dimension), 'scale': np.ones(dimension)}


def select_heads(x, y, split):
    """Two independent native-MSE choices; no mapped/outer target selection."""
    candidates = grid()
    pooled = np.empty((len(y), len(candidates) + 1, 2))
    for fold in sorted(set(split)):
        train, valid = split != fold, split == fold
        models = [constant(y[train], x.shape[1]), *learner.fit_models(x[train], y[train], candidates)]
        for index, model in enumerate(models):
            pooled[valid, index] = native_predict(model, x[valid])
    losses = np.mean((pooled - y[:, None, :]) ** 2, axis=0)
    chosen = losses.argmin(axis=0)
    full = [constant(y, x.shape[1]), *learner.fit_models(x, y, candidates)]
    heads = []
    for column, index in enumerate(chosen):
        model = full[index]
        head = ({**model, 'value': model['value'][column:column + 1]}
                if model['kind'] == 'constant' else learner.column_model(model, column))
        heads.append(learner.serial(head))
    return heads, {'selected': chosen.tolist(), 'inner_native_mse': losses.tolist(),
                   'specs': [{'family': 'mean'}, *candidates]}


def head_predictions(heads, x, independent=False):
    function = replay if independent else native_predict
    return np.column_stack([function(head, x)[:, 0] for head in heads])


def freeze(directory, local):
    if directory.exists():
        raise ValueError('Preserve earlier evidence; choose a new directory')
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    for path, receipt in ((ROOT / 'common-target-1', 'focus.receipt.json'),
                          (ROOT / 'cruise-baselines-1', 'evaluation.receipt.json'),
                          (BROWSER, 'evaluation.receipt.json')):
        check_receipt(path, receipt)
        ref(path / receipt)
        ref(path / 'plan.json')
    check_receipt(local, 'receipt.json', False)
    ref(local / 'receipt.json')
    identities = load(ref(local / 'identities.json'))
    original_plan = load(ref(ROOT / 'round-1/plan.json'))
    meta_path = ref(ROOT / f'round-1/inputs/{SOURCE}.json')
    data_path = ref(ROOT / f'round-1/inputs/{SOURCE}.npz')
    for path in (meta_path, data_path):
        if digest(path) != original_plan['inputs'][str(path.relative_to(ROOT / 'round-1'))]:
            raise ValueError('Original source input changed')
    meta, data = load(meta_path), arrays(data_path)
    columns = [meta['target_names'].index(name) for name in TARGETS]
    group_guard(validation_groups(meta['rows']), data['outer'])
    retained, exposure = exclusion(meta['rows'], identities)
    if retained.sum() < 30:
        raise ValueError('Insufficient locally disjoint native rows')
    browser_plan = load(ref(BROWSER / 'plan.json'))
    browser_meta = load(ref(BROWSER / f'inputs/{SOURCE}.json'))
    if browser_meta['rows'] != meta['rows']:
        raise ValueError('Browser and historical row identity differ')
    cases = [case for case in browser_plan['cases'] if case['cohort'] == SOURCE]
    browser = []
    for index, case in enumerate(cases):
        if case['index'] != index or case['id'] != meta['rows'][index]['id']:
            raise ValueError('Browser recording order differs')
        parent = BROWSER / f'cases/{SOURCE}/{index:04d}'
        receipt = load(ref(parent / 'receipt.json'))
        if receipt['plan_sha256'] != digest(BROWSER / 'plan.json'):
            raise ValueError('Browser receipt is for another plan')
        path = ref(parent / 'report.json')
        if digest(path) != receipt['outputs']['report.json']:
            raise ValueError('Browser cache changed')
        result = load(path)
        if result['case'] != case or len(result['chunks']) != 1:
            raise ValueError('Browser excerpt identity differs')
        chunk = result['chunks'][0]
        if chunk['start'] != case['starts'][0]:
            raise ValueError('Browser excerpt start differs')
        browser.append(chunk['browser']['embedding'])
    browser = np.asarray(browser)
    if browser.shape != data['embedding'].shape or not np.isfinite(browser).all():
        raise ValueError('Browser embeddings incomplete')
    baseline = arrays(ref(ROOT / 'cruise-baselines-1/external-predictions.npz'))
    browser_scores = arrays(ref(BROWSER / 'predictions.npz'))
    prior_path = ref(ROOT / f'round-1/inputs/{SOURCE}.prior-oof.npz')
    if digest(prior_path) != original_plan['inputs'][str(prior_path.relative_to(ROOT / 'round-1'))]:
        raise ValueError('Prior OOF input changed')
    prior = arrays(prior_path)['independent'][:, columns]
    observed = data['y'][:, columns].mean(axis=1)
    prefix = SOURCE + '__concentrate__'
    if not np.array_equal(observed, baseline[prefix + 'observed']) or not np.array_equal(observed, browser_scores[prefix + 'observed']):
        raise ValueError('Reference mappings differ')
    rows = [row for row, keep in zip(meta['rows'], retained, strict=True) if keep]
    groups = np.asarray(validation_groups(meta['rows']))[retained]
    outer = data['outer'][retained]
    inner = np.full((len(rows), 6), -1, dtype=int)
    for fold in range(5):
        train = outer != fold
        inner[train, fold] = folds(groups[train], count=3, seed=SEED)
        group_guard(groups[train], inner[train, fold])
    inner[:, 5] = folds(groups, count=3, seed=SEED)
    common = baseline[SOURCE + '__retained'][retained]
    if not np.array_equal(baseline[SOURCE + '__retained'], browser_scores[SOURCE + '__retained']):
        raise ValueError('Baseline disjoint masks differ')
    directory.mkdir(parents=True)
    values = {'embedding': data['embedding'][retained], 'attributes': data['attributes'][retained],
              'browser': browser[retained], 'y': data['y'][retained][:, columns], 'outer': outer,
              'inner': inner, 'common': common, 'prior_native': prior[retained],
              'original_indices': np.flatnonzero(retained)}
    values.update({method: baseline[prefix + method][retained]
                   for method in ('checkpoint', 'refresh_calibration', 'refresh_both')})
    values['browser_shared'] = browser_scores[prefix + 'browser_shared'][retained]
    np.savez_compressed(directory / 'inputs.npz', **values)
    save(directory / 'metadata.json', {'rows': rows, 'groups': groups.tolist(), 'exclusions': exposure,
         'original_rows': len(meta['rows']), 'native_rows': len(rows), 'common_rows': int(common.sum()),
         'attribute_names': original_plan['attribute_names'], 'target_names': TARGETS})
    for source, name in ((ROOT / 'common-target-1/focus-candidate.json', 'prior.json'),
                         (ROOT / 'cruise-baselines-1/checkpoint.json', 'checkpoint.json'),
                         (ROOT / 'cruise-baselines-1/refresh_calibration.json', 'refresh_calibration.json'),
                         (ROOT / 'cruise-baselines-1/refresh_both.json', 'refresh_both.json')):
        shutil.copyfile(ref(source), directory / name)
    if load(directory / 'prior.json')['target_names'] != TARGETS:
        raise ValueError('Prior native head ordering differs')
    code = source_paths()
    for relative, source in code.items():
        target = directory / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    plan = {'created_at': stamp(), 'ticket': 576, 'runtime': runtime(), 'seed': SEED,
            'grid': grid(), 'methods': METHODS, 'mapping': 'equal mean Reading/Studying vote fractions',
            'embedding_signature': original_plan['embedding_signature'], 'bootstrap_draws': 1000,
            'local_directory': str(local), 'references': references,
            'inputs': {p.name: digest(p) for p in directory.iterdir() if p.is_file()},
            'code': {relative: digest(source) for relative, source in code.items()},
            'budgets': {'artifact_bytes': 262144, 'warm_three_excerpt_ms': 100, 'process_rss_bytes': 1073741824},
            'limits': ['Previously inspected source and target; no pristine confirmation.',
                       'CAL500 labels describe whole songs, cached audio one ten-second excerpt.',
                       'Browser cache has embeddings only; no mixed-preparation combined model.',
                       'Identity exclusions cover known identities, not unknown aliases/backbone pretraining.',
                       'Local unselected vibes are unknown, not negative labels.',
                       'Paired intervals condition on fixed models/folds and prior inspection.']}
    save(directory / 'plan.json', plan)
    validate(directory, execution=True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'native_rows': len(rows), 'common_rows': int(common.sum())}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    if execution and plan['runtime'] != runtime():
        raise ValueError('Runtime changed since freeze')
    for name, expected in plan['inputs'].items():
        if digest(directory / name) != expected:
            raise ValueError('Frozen input changed: ' + name)
    for relative, expected in plan['code'].items():
        if digest(directory / 'source' / relative) != expected:
            raise ValueError('Archived source changed: ' + relative)
        if execution and digest(EXPERIMENTS / relative) != expected:
            raise ValueError('Executing source changed; preserve the round: ' + relative)
    for path, expected in plan['references'].items():
        if digest(path) != expected:
            raise ValueError('Referenced evidence changed: ' + path)
    return plan


def fit(directory):
    plan = validate(directory, execution=True)
    if (directory / 'fit.receipt.json').exists():
        raise ValueError('Fit already complete; preserve it')
    data = arrays(directory / 'inputs.npz')
    meta = load(directory / 'metadata.json')
    data['combined'] = np.column_stack([data['attributes'], data['embedding']])
    group_guard(meta['groups'], data['outer'])
    outputs, predictions, traces, replay_errors = {}, {}, {}, {}
    for method, family in METHODS.items():
        x, y = data[family], data['y']
        predicted = np.zeros_like(y)
        traces[method] = {}
        for fold in range(5):
            train, valid = data['outer'] != fold, data['outer'] == fold
            heads, trace = select_heads(x[train], y[train], data['inner'][train, fold])
            predicted[valid] = head_predictions(heads, x[valid])
            traces[method][str(fold)] = trace
        heads, traces[method]['full'] = select_heads(x, y, data['inner'][:, 5])
        artifact = {'format': 'focus-native-v1', 'vibe': 'concentrate', 'target_names': TARGETS,
                    'family': family, 'mapping': plan['mapping'], 'heads': heads,
                    'embedding_signature': plan['embedding_signature'], 'attribute_names': meta['attribute_names'] if family == 'combined' else [],
                    'plan_sha256': digest(directory / 'plan.json'), 'training_rows': len(y)}
        path = directory / (method + '.json')
        # Compact serialization is part of the measured artifact, no coefficient rounding.
        path.write_text(json.dumps(artifact, separators=(',', ':'), allow_nan=False) + '\n')
        reloaded = load(path)
        first = head_predictions(reloaded['heads'], x)
        second = head_predictions(reloaded['heads'], x, independent=True)
        error = np.max(np.abs(first - second), axis=0)
        if np.any(error > 1e-10):
            raise ValueError('Per-head export replay differs')
        replay_errors[method] = error.tolist()
        predictions[method] = predicted
        outputs[path.name] = digest(path)
        print(json.dumps({'fitted': method, 'rows': len(y), 'bytes': path.stat().st_size,
                          'per_head_replay_error': error.tolist()}), flush=True)
    for method in ('training_mean', 'training_median', 'acoustic_ridge'):
        predictions[method] = np.zeros_like(data['y'])
        for fold in range(5):
            train, valid = data['outer'] != fold, data['outer'] == fold
            if method == 'acoustic_ridge':
                model = learner.fit_models(data['attributes'][train], data['y'][train], [{'family': 'ridge', 'alpha': 10.}])[0]
                predictions[method][valid] = learner.predict(model, data['attributes'][valid])
            else:
                operation = np.mean if method == 'training_mean' else np.median
                predictions[method][valid] = operation(data['y'][train], axis=0)
    prior = load(directory / 'prior.json')
    replay_errors['prior'] = np.max(np.abs(head_predictions(prior['heads'], data['embedding']) -
                                         head_predictions(prior['heads'], data['embedding'], True)), axis=0).tolist()
    if max(replay_errors['prior']) > 1e-10:
        raise ValueError('Prior native head replay differs')
    predictions['prior'] = data['prior_native']
    np.savez_compressed(directory / 'oof.npz', **predictions)
    save(directory / 'fit.report.json', {'selection': traces, 'per_head_replay_error': replay_errors,
         'native_targets': TARGETS, 'quality_uses': 'outer held-out predictions, never full-export training replay'})
    outputs.update({name: digest(directory / name) for name in ('oof.npz', 'fit.report.json')})
    validate(directory, execution=True)
    save(directory / 'fit.receipt.json', {'plan_sha256': digest(directory / 'plan.json'), 'outputs': outputs})


def calibration(y, prediction):
    bins = np.minimum((np.clip(prediction, 0, 1) * 5).astype(int), 4)
    return [{'lower': i / 5, 'upper': (i + 1) / 5, 'rows': int((bins == i).sum()),
             'score_mean': float(prediction[bins == i].mean()) if (bins == i).any() else None,
             'reference_mean': float(y[bins == i].mean()) if (bins == i).any() else None}
            for i in range(5)]


def brief_metrics(y, prediction, split):
    """Same tie-aware fold definition as summary, without unused correlations."""
    result = []
    for fold in sorted(set(split)):
        observed, score = y[split == fold], prediction[split == fold]
        k = top_count(len(observed))
        ideal, _ = discounted_gain(observed, observed, k)
        gain, top = discounted_gain(observed, score, k)
        result.append([gain / ideal if ideal > 0 and np.ptp(observed) > 1e-12 else np.nan,
                       top - observed.mean(), np.mean((observed - score) ** 2)])
    result = np.asarray(result)
    return np.array([np.nanmean(result[:, 0]) if np.isfinite(result[:, 0]).any() else np.nan,
                     *result[:, 1:].mean(axis=0)])


def paired(y, scores, split, groups, draws=1000):
    group_guard(groups, split)
    groups = np.asarray(groups)
    blocks = [[np.flatnonzero((groups == g) & (split == fold))
               for g in sorted(set(groups[split == fold]))] for fold in sorted(set(split))]
    keys = list(scores)
    boot = {name: [] for name in keys}
    rng = np.random.default_rng(SEED)
    for _ in range(draws):
        index = np.concatenate([np.concatenate([b[i] for i in rng.integers(len(b), size=len(b))]) for b in blocks])
        for name in keys:
            boot[name].append(brief_metrics(y[index], scores[name][index], split[index]))
    boot = {name: np.asarray(value) for name, value in boot.items()}
    contrasts = {}
    for method in METHODS:
        for control in ('prior', 'checkpoint', 'refresh_calibration', 'refresh_both', 'browser_shared', 'training_mean'):
            point = brief_metrics(y, scores[method], split) - brief_metrics(y, scores[control], split)
            delta = boot[method] - boot[control]
            contrasts[method + '_vs_' + control] = {}
            for column, metric in enumerate(('ndcg', 'top_lift', 'mse')):
                valid = delta[:, column][np.isfinite(delta[:, column])]
                contrasts[method + '_vs_' + control][metric] = {
                    'difference': float(point[column]) if np.isfinite(point[column]) else None,
                    'conditional_95_percent_interval': np.quantile(valid, [.025, .975]).tolist() if len(valid) else None,
                    'usable_draws': len(valid), 'requested_draws': draws}
    return contrasts


def product_score(model, x):
    for key in ('native', 'calibration'):
        stage = model[key]
        x = replay({'kind': 'ridge', 'medians': stage['mean'], **stage}, x)
    x = np.maximum(x, 1e-8)
    return (x / x.sum(axis=1, keepdims=True))[:, model['labels'].index('concentrate')]


def local_diagnostics(directory, plan):
    local = inside(plan['local_directory'])
    check_receipt(local, 'receipt.json', False)
    rows = load(local / 'labels.json')['rows']
    source_rows = load(ROOT / f'round-1/inputs/{SOURCE}.json')['rows']
    prior_eligible, exposure = exclusion(rows, source_rows)
    models = {name: load(directory / (name + '.json'))
              for name in ('prior', 'historical_embedding', 'browser_embedding')}
    shared = load(directory / 'checkpoint.json')
    records = []
    for index, row in enumerate(rows):
        samples = [load(local / name) for name in row['sample_files']]
        x = np.asarray([sample['embedding']['values'] for sample in samples])
        if row['embedding_signature'] != plan['embedding_signature'] or x.shape != (len(samples), 200):
            records.append({'id': row['id'], 'unsupported': 'incompatible embedding'})
            continue
        values = {}
        for method, model in models.items():
            native = head_predictions(model['heads'], x, independent=True)
            values[method] = {'first_excerpt': float(native[0].mean()),
                             'all_available': float(native.mean()),
                             'excerpt_scores': native.mean(axis=1).tolist(),
                             'first_to_all_difference': float(native.mean() - native[0].mean())}
        values['checkpoint'] = {'first_excerpt': float(product_score(shared, x[:1])[0]),
                                'all_available': float(product_score(shared, x.mean(axis=0, keepdims=True))[0])}
        records.append({'id': row['id'], 'samples': len(samples), 'positions': [s['position_seconds'] for s in samples],
                        'human_focus_positive': 'concentrate' in row['labels'],
                        'prior_inspected': row['prior_inspected'], 'prior_native_identity_disjoint': bool(prior_eligible[index]),
                        'scores': values})
    positive = [row for row in records if row.get('human_focus_positive')]
    return {'rows': len(rows), 'compatible': sum('scores' in row for row in records),
            'positive_focus_rows': len(positive), 'new_positive_focus_rows': sum(not r['prior_inspected'] for r in positive),
            'explicit_negative_focus_rows': 0, 'prior_native_exclusions': exposure,
            'partial_capture_rows': sum(row.get('samples', 0) in (1, 2) for row in records),
            'records': records, 'quality_claim': 'Positive support and capture consistency only; unselected vibes and distraction are unknown.',
            'combined_model': 'Not applied: current capture acoustic schema has not been verified against historical features.'}


def evaluate(directory):
    plan = validate(directory, execution=True)
    check_receipt(directory, 'fit.receipt.json')
    if (directory / 'evaluation.receipt.json').exists():
        raise ValueError('Evaluation already complete; preserve it')
    data, native = arrays(directory / 'inputs.npz'), arrays(directory / 'oof.npz')
    meta = load(directory / 'metadata.json')
    keep, split = data['common'], data['outer'][data['common']]
    groups = np.asarray(meta['groups'])[keep]
    y = data['y'][keep].mean(axis=1)
    scores = {name: value[keep].mean(axis=1) for name, value in native.items()}
    scores.update({name: data[name][keep] for name in ('checkpoint', 'refresh_calibration', 'refresh_both', 'browser_shared')})
    metrics = {name: {**summary(y, value, split), 'score_mean': float(value.mean()),
                     'reference_mean': float(y.mean()), 'calibration': calibration(y, value)} for name, value in scores.items()}
    native_metrics = {name: {target: {'mse': float(np.mean((data['y'][keep, c] - value[keep, c]) ** 2)),
                                    'mae': float(np.mean(np.abs(data['y'][keep, c] - value[keep, c])))}
                            for c, target in enumerate(TARGETS)} for name, value in native.items()}
    print(json.dumps({'same_row_quality': {name: value['ndcg'] for name, value in metrics.items()},
                      'rows': int(keep.sum()), 'paired_draws': plan['bootstrap_draws']}), flush=True)
    differences = paired(y, scores, split, groups, plan['bootstrap_draws'])
    resource_results = {}
    for name, family in {'prior': 'embedding', **METHODS}.items():
        path = directory / (name + '.json')
        x = np.column_stack([data['attributes'], data['embedding']]) if family == 'combined' else data[family]
        before = time.perf_counter()
        model = load(path)
        load_ms = (time.perf_counter() - before) * 1000
        head_predictions(model['heads'], x[:3])
        timings = []
        for _ in range(100):
            before = time.perf_counter()
            head_predictions(model['heads'], x[:3])
            timings.append((time.perf_counter() - before) * 1000)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        resource_results[name] = {'artifact_bytes': path.stat().st_size, 'load_ms': load_ms,
            'warm_three_excerpt_median_ms': float(np.median(timings)), 'warm_three_excerpt_max_ms': max(timings),
            'process_peak_rss_bytes': rss,
            'budget_pass': path.stat().st_size <= plan['budgets']['artifact_bytes'] and max(timings) <= plan['budgets']['warm_three_excerpt_ms'] and rss <= plan['budgets']['process_rss_bytes'],
            'scope': 'Python in-process predictor; excludes encoder and application startup'}
    local = local_diagnostics(directory, plan)
    save(directory / 'local.report.json', local)
    report = {'created_at': stamp(), 'plan_sha256': digest(directory / 'plan.json'),
              'native_rows': len(data['y']), 'common_rows': int(keep.sum()), 'groups': len(set(groups)),
              'metrics': metrics, 'native_metrics': native_metrics, 'paired': differences,
              'resources': resource_results, 'local_summary': {k: v for k, v in local.items() if k != 'records'},
              'limits': plan['limits'], 'no_automatic_adoption': True,
              'decision': 'Await explicit report reconciliation of all declared procedures and reference audit.'}
    save(directory / 'evaluation.report.json', report)
    np.savez_compressed(directory / 'evaluation.npz', observed=y, outer=split, **scores)
    validate(directory, execution=True)
    save(directory / 'evaluation.receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
         'fit_receipt_sha256': digest(directory / 'fit.receipt.json'),
         'outputs': {name: digest(directory / name) for name in ('evaluation.report.json', 'evaluation.npz', 'local.report.json')}})
    print(json.dumps({'evaluated': str(directory), 'resources': resource_results,
                      'local_rows': local['rows'], 'positive_focus_rows': local['positive_focus_rows']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'fit', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--local', type=Path, default=LOCAL)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory, inside(args.local))
        elif args.action == 'fit':
            fit(directory)
        elif args.action == 'evaluate':
            evaluate(directory)
        else:
            validate(directory)
            for stage in ('fit', 'evaluation'):
                check_receipt(directory, stage + '.receipt.json')
            print(json.dumps({'verified': str(directory)}))


if __name__ == '__main__':
    main()
