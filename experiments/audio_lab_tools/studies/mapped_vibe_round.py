"""Externally supervised attribute predictors composed with fixed vibe rules.

No local outcomes are loaded. All fitting, feature preprocessing and selection
are inside grouped training folds. Completed stage receipts are checksummed.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

import numpy as np
from scipy.spatial.distance import cdist
from threadpoolctl import threadpool_limits

from .. import TOOLS, source_path
from . import vibe_specialist_models as learner
from ..support.external_experiment import LAB, OUT, digest, save, stamp
from ..training.external_training import folds, metrics as native_metrics
from .mapped_vibe_inference import feature_values, replay, score
from ..evaluation.mapped_vibe_metrics import choose, summary
from .mapped_vibe_rules import ANCHORS, DATASETS, TRANSFER, VIBES, apply, labels, registry
from .vibe_specialist_round import (EXPERIMENTS, input_arrays, inside, load, npz,
                                    receipt_valid, stage, versions)

PRIOR = LAB / 'local/per-vibe-20261029/all-sources-1'
DEFAULT = LAB / 'local/mapped-vibes-20261029/round-1'
FAMILIES = ('attributes', 'embedding', 'attributes_embedding')
SEED = 53030


def catalogue():
    return ([{'features': 'attributes', 'learner': {'family': name}} for name in ('mean', 'median')]
            + [{'features': family, 'learner': spec} for family in FAMILIES for spec in learner.grid()])


def sources():
    paths = [*TOOLS.rglob('mapped_vibe_*.py'), *TOOLS.rglob('__init__.py'),
             *(source_path(name) for name in (
                 'test_mapped_vibes.py', 'vibe_specialist_models.py', 'vibe_specialist_round.py',
                 'transfer_identity.py', 'library_replay.py', 'consumer_replay.py',
                 'replay_runtime.py', 'replay_storage.py')), EXPERIMENTS / 'run.py']
    paths += list(TOOLS.rglob('external_*.py'))
    return {str(p.relative_to(EXPERIMENTS)): digest(p) for p in sorted(set(paths))}


def split_arrays(groups, outer):
    groups = np.asarray(groups)
    inner = np.full((len(groups), 6), -1, dtype=int)
    for f in range(5):
        train = outer != f
        inner[train, f] = folds(groups[train], count=3, seed=SEED + 1)
    inner[:, 5] = folds(groups, count=3, seed=SEED + 1)
    return inner


def freeze(directory):
    if (directory / 'plan.json').exists():
        return validate(directory)
    if directory.exists() and list(directory.iterdir()):
        raise ValueError('Preserve the nonempty directory and choose another --directory')
    old = load(PRIOR / 'plan.json')
    rules, counts, references, prior_oof = registry(), {}, {}, {}
    directory.mkdir(parents=True, exist_ok=True)
    attribute_names = None
    for dataset in DATASETS:
        meta_path = PRIOR / f'inputs/{dataset}.json'
        metadata = load(meta_path, old['inputs'][f'inputs/{dataset}.json'])
        data_path = PRIOR / f'inputs/{dataset}.npz'
        if digest(data_path) != old['inputs'][f'inputs/{dataset}.npz']:
            raise ValueError('Previous native arrays changed')
        previous = input_arrays(data_path)
        index_path = OUT / f'{dataset}.features.json'
        index = load(index_path, metadata['source_sha256'])
        lookup = {row['id']: row for row in index['rows']}
        attributes, embedding = [], []
        for row in metadata['rows']:
            cached = lookup[row['id']]
            feature_path = inside(cached['feature_file'])
            bundle = load(feature_path, metadata['feature_files'][str(feature_path)])
            values = bundle['audio_attributes']['values']
            if attribute_names is None:
                attribute_names = sorted(values)
            if sorted(values) != attribute_names:
                raise ValueError('Acoustic attribute schema changed: ' + row['id'])
            attributes.append([values[name] if values[name] is not None else np.nan for name in attribute_names])
            embedding.append(bundle['prediction']['embedding']['values'])
        if not np.array_equal(np.array(embedding), previous['x']):
            raise ValueError('Cached embeddings differ from protected native inputs')
        for record in rules[dataset].values():
            if not labels(record['expression']).issubset(metadata['target_names']):
                raise ValueError('Rule requires unavailable labels: ' + dataset)
        xattr = np.array(attributes, dtype=float)
        outer = previous['outer']  # Same label-independent groups/rows for prior OOF comparisons.
        npz(directory / f'inputs/{dataset}.npz', attributes=xattr, embedding=previous['x'],
            y=previous['y'], outer=outer, inner=split_arrays(metadata['groups'], outer))
        save(directory / f'inputs/{dataset}.json', metadata)
        counts[dataset] = {'rows': len(outer), 'groups': len(set(metadata['groups'])),
                           'vibes': list(rules[dataset]), 'annotation_units': sorted({r['annotation_unit'] for r in metadata['rows']}),
                           'attribute_missing_values': int((~np.isfinite(xattr)).sum()),
                           'excluded_protected_ids': metadata['excluded_ids']}
        name = 'native-' + dataset
        if not receipt_valid(PRIOR, name, digest(PRIOR / 'plan.json')):
            raise ValueError('Previous native OOF baseline not verified: ' + dataset)
        previous_oof = PRIOR / f'{name}.oof.npz'
        shutil.copyfile(previous_oof, directory / f'inputs/{dataset}.prior-oof.npz')
        prior_oof[dataset] = digest(previous_oof)
        references[str(meta_path)] = digest(meta_path)
        references[str(data_path)] = digest(data_path)
        references[str(index_path)] = digest(index_path)
        print(json.dumps({'freeze': dataset, **counts[dataset]}), flush=True)
    # Only identity protection metadata is copied. Never open local.npz or any
    # historical/new local human outcome file in this experiment.
    identities = load(PRIOR / 'inputs/local.json', old['inputs']['inputs/local.json'])['protected_identities']
    save(directory / 'inputs/protected-identities.json', identities)
    save(directory / 'inputs/rules.json', rules)
    for relative, expected in sources().items():
        destination = directory / 'source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXPERIMENTS / relative, destination)
        if digest(destination) != expected:
            raise ValueError('Research code changed while freezing')
    from ..replay.mapped_vibe_consumer import inputs as consumer_inputs
    plan = {'format': 'external-attributes-fixed-vibes-v1', 'created_at': stamp(),
            'datasets': counts, 'attribute_names': attribute_names,
            'embedding_signature': old['embedding_signature'], 'grid': catalogue(),
            'seed': SEED, 'outer': '5 existing grouped folds, identical rows to prior native OOF',
            'inner': '3 label-independent grouped folds on each outer training partition; explicit arrays frozen',
            'primary': 'mean validation-fold linear-gain tie-aware NDCG at min(10,ceil(.2*n))',
            'selection': 'NDCG, then MSE, then grid order; each vibe independently; no outer outcomes',
            'native_loss': 'MSE: proper squared loss for continuous conditional means and marginal vote/class probabilities; fractions preserved',
            'methods': ['training_mean', 'training_median', *FAMILIES, 'selected', 'shared', 'native_mse', 'prior_embedding_mae'],
            'anchors': ANCHORS, 'transfer': TRANSFER,
            'consumer': consumer_inputs(),
            'transfer_ensemble': 'equal mean of all eligible other sources in the same predeclared VA or GEMS family; target outcomes never set weights',
            'local_outcomes_loaded': False, 'local_predictions_as_truth': False,
            'protected_identities': len(identities), 'prior_native_oof': prior_oof,
            'references': references, 'versions': versions(), 'code': sources(),
            'inputs': {str(p.relative_to(directory)): digest(p) for p in sorted((directory / 'inputs').iterdir())},
            'product_adoption': False,
            'limits': ['External references use the same declared rule on human annotations, not newly collected direct judgments for every composite.',
                       'Cached audio is unchanged; source alignment and missing artist identities retain their original limitations.',
                       'Existing corpora were inspected in earlier experiments; new objectives are predeclared but these are not untouched new confirmation corpora.',
                       'VGMIDI has five franchise groups; nested training uses only three to four franchises.',
                       'Upstream MusiCNN training overlap is unresolved; no pretrained mood heads enter these features.',
                       'Cross-corpus scale and annotation differences remain even with aligned rules.']}
    save(directory / 'plan.json', plan)
    return plan


def validate(directory):
    plan = load(directory / 'plan.json')
    if plan['code'] != sources() or plan['versions'] != versions():
        raise ValueError('Frozen code/runtime changed; preserve round and use a new directory')
    for relative, expected in plan['inputs'].items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Frozen input changed: ' + relative)
    for relative, expected in plan['code'].items():
        if digest(inside(directory / 'source' / relative)) != expected:
            raise ValueError('Archived source changed: ' + relative)
    return plan


def subset(data, mask):
    return {key: data[key][mask] for key in ('attributes', 'embedding')}


def constant(y, dimension, method):
    return {'kind': 'constant', 'value': (y.mean(0) if method == 'mean' else np.median(y, axis=0)),
            'medians': np.zeros(dimension), 'mean': np.zeros(dimension), 'scale': np.ones(dimension)}


def fit_choices(data, y, indices, candidates):
    result = {}
    for family in FAMILIES:
        x = feature_values(data, family)
        ids = sorted({i for i in indices if candidates[i]['features'] == family})
        normal = [i for i in ids if candidates[i]['learner']['family'] not in ('mean', 'median')]
        result.update(zip(normal, learner.fit_models(x, y, [candidates[i]['learner'] for i in normal])))
        for i in set(ids) - set(normal):
            result[i] = constant(y, x.shape[1], candidates[i]['learner']['family'])
    return result


def predict_model(model, x):
    if model['kind'] == 'constant':
        return np.broadcast_to(model['value'], (len(x), len(model['value']))).copy()
    return learner.predict(model, x)


def predict_choices(models, candidates, data):
    """Share neighbor distances and sorting across the eight KNN candidates."""
    result = {}
    for family in FAMILIES:
        ids = [i for i in models if candidates[i]['features'] == family]
        if not ids:
            continue
        x = feature_values(data, family)
        neighbors = [i for i in ids if models[i]['kind'] == 'neighbors']
        for i in set(ids) - set(neighbors):
            result[i] = predict_model(models[i], x)
        if neighbors:
            first = models[neighbors[0]]
            distance = cdist(learner.scaled(first, x), np.asarray(first['training_x']), metric='sqeuclidean')
            ordered = np.argsort(distance, axis=1, kind='stable')[:, :max(models[i]['neighbors'] for i in neighbors)]
            for i in neighbors:
                model = models[i]
                selected = ordered[:, :model['neighbors']]
                d = np.take_along_axis(distance, selected, axis=1)
                weights = np.ones_like(d)
                if model['weighting'] == 'inverse_distance':
                    weights = 1 / np.maximum(np.sqrt(d), 1e-8)
                    exact = d <= 1e-16
                    weights = np.where(exact.any(1, keepdims=True), exact.astype(float), weights)
                result[i] = (np.asarray(model['training_y'])[selected] * weights[:, :, None]).sum(1) / weights.sum(1, keepdims=True)
    return result


def pool_oof(data, y, split, candidates):
    result = np.zeros((len(y), len(candidates), y.shape[1]))
    for fold in sorted(set(split)):
        train, valid = split != fold, split == fold
        models = fit_choices(subset(data, train), y[train], range(len(candidates)), candidates)
        values = predict_choices(models, candidates, subset(data, valid))
        for index, prediction in values.items():
            result[valid, index] = prediction
    return result


def selection(y, pool, split, names, rules, candidates):
    native_ids = np.argmin(np.mean((pool - y[:, None, :]) ** 2, axis=0), axis=0).tolist()
    choices, evidence = {}, {}
    per_vibe = []
    for vibe, rule in rules.items():
        observed = apply(rule['expression'], y, names)
        predictions = apply(rule['expression'], pool, names)
        methods = {}
        for family in FAMILIES:
            allowed = [0, 1] + [i for i, c in enumerate(candidates) if c['features'] == family and i > 1]
            methods[family], _ = choose(observed, predictions, split, allowed)
        methods['selected'], evidence[vibe] = choose(observed, predictions, split)
        choices[vibe] = methods
        if vibe in VIBES:
            per_vibe.append([evidence[vibe][str(i)]['ndcg'] for i in range(len(candidates))])
    def shared_key(i):
        values = [row[i] for row in per_vibe if row[i] is not None]
        return (round(float(np.mean(values)), 12) if values else -1., -i)
    shared = max(range(len(candidates)), key=shared_key)
    for methods in choices.values():
        methods.update(shared=shared, training_mean=0, training_median=1)
    return {'vibes': choices, 'native_mse': native_ids, 'inner_ranking': evidence}


def required_ids(selected):
    return sorted(set(selected['native_mse']) | {i for v in selected['vibes'].values() for i in v.values()})


def composed_predictions(selected, values, yshape, rules, names):
    native = np.column_stack([values[i][:, j] for j, i in enumerate(selected['native_mse'])])
    result = {}
    for vibe, methods in selected['vibes'].items():
        expression = rules[vibe]['expression']
        result[vibe] = {method: apply(expression, values[index], names) for method, index in methods.items()}
        result[vibe]['native_mse'] = apply(expression, native, names)
    if native.shape != yshape:
        raise ValueError('Native output shape mismatch')
    return result, native


def expert_record(dataset, vibe, model, index, candidates, names, rule, plan_sha):
    return {'source': dataset, 'vibe': vibe, 'family': candidates[index]['features'],
            'candidate': index, 'spec': candidates[index], 'target_names': names,
            'rule': rule, 'model': learner.serial(model), 'plan_sha256': plan_sha,
            'local_labels_used': False}


def dataset_stage(directory, dataset):
    plan = load(directory / 'plan.json')
    candidates = plan['grid']
    metadata = load(directory / f'inputs/{dataset}.json')
    data = input_arrays(directory / f'inputs/{dataset}.npz')
    y, outer, names = data['y'], data['outer'], metadata['target_names']
    rules = load(directory / 'inputs/rules.json')[dataset]
    predictions = {vibe: {method: np.zeros(len(y)) for method in plan['methods']} for vibe in rules}
    native = np.zeros_like(y)
    decisions = []
    for fold in range(5):
        train, valid = outer != fold, outer == fold
        training = subset(data, train)
        pool = pool_oof(training, y[train], data['inner'][train, fold], candidates)
        selected = selection(y[train], pool, data['inner'][train, fold], names, rules, candidates)
        fitted = fit_choices(training, y[train], required_ids(selected), candidates)
        values = predict_choices(fitted, candidates, subset(data, valid))
        composed, native[valid] = composed_predictions(selected, values, y[valid].shape, rules, names)
        for vibe, methods in composed.items():
            for method, value in methods.items():
                predictions[vibe][method][valid] = value
        decisions.append({'outer_fold': fold, **selected})
        print(json.dumps({'dataset': dataset, 'outer_fold': fold, 'mapped_vibes': len(rules)}), flush=True)
    prior = input_arrays(directory / f'inputs/{dataset}.prior-oof.npz')['independent']
    report = {'dataset': dataset, 'rows': len(y), 'groups': len(set(metadata['groups'])),
              'references': 'same frozen rule applied to held-out external annotations',
              'native_mse': native_metrics(y, native, names), 'vibes': {}, 'fold_selection': decisions,
              'plan_sha256': digest(directory / 'plan.json')}
    report['native_mse']['proper_loss_by_target'] = {name: float(np.mean((y[:, j] - native[:, j]) ** 2))
                                                    for j, name in enumerate(names)}
    arrays = {'outer': outer, 'native_observed': y, 'native_predicted': native}
    row_predictions = []
    for vibe, rule in rules.items():
        observed = apply(rule['expression'], y, names)
        predictions[vibe]['prior_embedding_mae'] = apply(rule['expression'], prior, names)
        report['vibes'][vibe] = {method: summary(observed, values, outer) for method, values in predictions[vibe].items()}
        arrays[vibe + '__observed'] = observed
        arrays.update({vibe + '__' + method: values for method, values in predictions[vibe].items()})
    for i, row in enumerate(metadata['rows']):
        row_predictions.append({'id': row['id'], 'group': metadata['groups'][i], 'outer_fold': int(outer[i]),
            'native_observed': y[i].tolist(), 'native_predicted': native[i].tolist(),
            'vibes': {v: {'observed': float(arrays[v + '__observed'][i]),
                         'predictions': {m: float(p[i]) for m, p in methods.items()}}
                      for v, methods in predictions.items()}})
    # Full-source fit for export: use only source grouped development folds.
    full_pool = pool_oof(data, y, data['inner'][:, 5], candidates)
    selected = selection(y, full_pool, data['inner'][:, 5], names, rules, candidates)
    fitted = fit_choices(data, y, required_ids(selected), candidates)
    experts, maximum = {}, 0.
    for vibe, rule in rules.items():
        index = selected['vibes'][vibe]['selected']
        experts[vibe] = expert_record(dataset, vibe, fitted[index], index, candidates, names, rule, report['plan_sha256'])
        expected = apply(rule['expression'], predict_model(fitted[index], feature_values(data, candidates[index]['features'])), names)
        maximum = max(maximum, float(np.max(np.abs(expected - score(experts[vibe], data)))))
    if maximum > 1e-10:
        raise ValueError('Independent serialized scorer replay mismatch')
    report['export_max_absolute_error'] = maximum
    report['full_source_selection'] = selected
    prefix = directory / dataset
    save(prefix.with_suffix('.report.json'), report)
    save(prefix.with_suffix('.experts.json'), {'experts': experts, 'selection': selected,
         'attribute_names': plan['attribute_names'], 'embedding_signature': plan['embedding_signature']})
    save(prefix.with_suffix('.predictions.json'), {'native_target_names': names, 'rows': row_predictions})
    npz(prefix.with_suffix('.oof.npz'), **arrays)
    return [prefix.with_suffix(s) for s in ('.report.json', '.experts.json', '.predictions.json', '.oof.npz')]


def disjoint(source, target):
    """Exclude full source identity components, plus conservative title rows."""
    from ..evaluation.transfer_identity import exclusion
    return exclusion(source, target)[0]


def transfer_stage(directory, source, target):
    plan = load(directory / 'plan.json')
    candidates = plan['grid']
    meta = load(directory / f'inputs/{source}.json')
    test_meta = load(directory / f'inputs/{target}.json')
    data = input_arrays(directory / f'inputs/{source}.npz')
    test = input_arrays(directory / f'inputs/{target}.npz')
    retained = disjoint(meta['rows'], test_meta['rows'])
    groups = np.asarray(meta['groups'])[retained]
    report = {'source': source, 'target': target, 'training_rows': int(retained.sum()),
              'target_rows': len(test['y']), 'excluded_ids': [r['id'] for r, keep in zip(meta['rows'], retained) if not keep],
              'known_artist_rows': {source: sum(bool(r.get('artist')) for r in meta['rows']),
                                    target: sum(bool(r.get('artist')) for r in test_meta['rows'])},
              'selection': 'source-only 3 grouped folds after removing all known target identity overlaps',
              'target_labels_used_for_selection': False, 'vibes': {}}
    prefix = directory / f'transfer-{source}__{target}'
    arrays = {}
    if retained.sum() < 30 or len(set(groups)) < 3:
        report['state'] = 'insufficient_disjoint_training_groups'
    else:
        training, y = subset(data, retained), data['y'][retained]
        split = folds(groups, count=3, seed=SEED + 1)
        pool = pool_oof(training, y, split, candidates)
        source_rules = load(directory / 'inputs/rules.json')[source]
        target_rules = load(directory / 'inputs/rules.json')[target]
        supported = {v: rule for v, rule in source_rules.items() if v in target_rules and v in VIBES}
        selected = selection(y, pool, split, meta['target_names'], supported, candidates)
        fitted = fit_choices(training, y, required_ids(selected), candidates)
        values = predict_choices(fitted, candidates, test)
        composed, _ = composed_predictions(selected, values, test['y'].shape[:1] + y.shape[1:], supported, meta['target_names'])
        arrays['outer'] = test['outer']
        for vibe, methods in composed.items():
            reference = apply(target_rules[vibe]['expression'], test['y'], test_meta['target_names'])
            report['vibes'][vibe] = {method: summary(reference, values, test['outer']) for method, values in methods.items()}
            arrays[vibe + '__observed'] = reference
            arrays.update({vibe + '__' + method: values for method, values in methods.items()})
        report.update(state='complete', source_selection=selected)
    save(prefix.with_suffix('.report.json'), report)
    npz(prefix.with_suffix('.oof.npz'), **arrays)
    return [prefix.with_suffix('.report.json'), prefix.with_suffix('.oof.npz')]


def export_stage(directory):
    plan = load(directory / 'plan.json')
    fingerprint = digest(directory / 'plan.json')
    for dataset in DATASETS:
        if not receipt_valid(directory, 'dataset-' + dataset, fingerprint):
            raise ValueError('Finish every source before export')
    experts = {v: load(directory / f'{source}.experts.json')['experts'][v] for v, source in plan['anchors'].items()}
    bundle = {'format': 'external-mapped-vibes-research-v1', 'plan_sha256': fingerprint,
              'attribute_names': plan['attribute_names'], 'embedding_signature': plan['embedding_signature'],
              'experts': experts, 'independent_scores': True, 'local_labels_used': False,
              'source_choice': 'predeclared semantic/provenance anchors, not cross-dataset score comparisons',
              'source_dependencies': {str(Path(f'{s}.experts.json')): digest(directory / f'{s}.experts.json') for s in set(plan['anchors'].values())},
              'product_adoption': False}
    export_path = directory / 'six-scorers.json'
    save(export_path, bundle)
    exported = load(export_path)
    data = input_arrays(directory / 'inputs/cal500-raw-activity.npz')
    measurements, columns = {}, []
    for vibe in VIBES:
        started = time.perf_counter()
        values = score(exported['experts'][vibe], data)
        elapsed = time.perf_counter() - started
        columns.append(values)
        measurements[vibe] = {'source': experts[vibe]['source'], 'spec': experts[vibe]['spec'],
                              'rows': len(values), 'wall_s': elapsed,
                              'model_bytes_json': len(json.dumps(experts[vibe]).encode())}
    # Changing a different exported scorer cannot renormalize other columns.
    before = np.column_stack(columns)
    after = np.column_stack([score(exported['experts'][v], data) for v in VIBES[1:]])
    error = float(np.max(np.abs(before[:, 1:] - after)))
    if error != 0 or not np.isfinite(before).all() or (before < 0).any() or (before > 1).any():
        raise ValueError('Export bounds/independent-score verification failed')
    save(directory / 'export.report.json', {'plan_sha256': fingerprint, 'bundle_sha256': digest(export_path),
         'bundle_bytes': export_path.stat().st_size, 'measurements': measurements,
         'independence_max_absolute_error': error, 'rows': len(before),
         'timing_scope': 'Python independent replay including model array materialization; not browser latency',
         'all_source_export_max_absolute_error': max(load(directory / f'{s}.report.json')['export_max_absolute_error'] for s in DATASETS)})
    return [export_path, directory / 'export.report.json']


def final_report(directory):
    fingerprint = digest(directory / 'plan.json')
    result = {'plan_sha256': fingerprint, 'datasets': {}, 'transfers': {}, 'ensembles': {},
              'local_labels_used': False, 'product_adoption': False}
    for dataset in DATASETS:
        if receipt_valid(directory, 'dataset-' + dataset, fingerprint):
            report = load(directory / f'{dataset}.report.json')
            result['datasets'][dataset] = {k: report[k] for k in ('rows', 'groups', 'vibes', 'native_mse', 'export_max_absolute_error')}
    for source, target in TRANSFER:
        name = f'transfer-{source}__{target}'
        if receipt_valid(directory, name, fingerprint):
            report = load(directory / f'{name}.report.json')
            result['transfers'][source + '__' + target] = {k: report[k] for k in ('state', 'training_rows', 'excluded_ids', 'vibes')}
    for target in sorted({b for _, b in TRANSFER}):
        reports = [input_arrays(directory / f'transfer-{source}__{target}.oof.npz')
                   for source, other in TRANSFER if other == target and source + '__' + target in result['transfers']
                   and result['transfers'][source + '__' + target]['state'] == 'complete']
        if len(reports) < 2:
            continue
        common = set.intersection(*(set(r) for r in reports))
        result['ensembles'][target] = {key.split('__')[0]: summary(reports[0][key.replace('__selected', '__observed')],
            np.mean([r[key] for r in reports], axis=0), reports[0]['outer']) for key in sorted(common) if key.endswith('__selected')}
    result['supported_pairs'] = sum(len(r['vibes']) for r in result['datasets'].values())
    result['consumer'] = load(directory / 'consumer.report.json') if (directory / 'consumer.report.json').exists() else None
    result['state'] = 'complete' if (len(result['datasets']) == len(DATASETS) and len(result['transfers']) == len(TRANSFER)
        and receipt_valid(directory, 'export', fingerprint) and receipt_valid(directory, 'consumer', fingerprint)) else 'partial'
    if result['state'] == 'complete' and result['consumer']['state'] != 'complete':
        result['state'] = 'complete_with_consumer_failure'
    save(directory / 'summary.json', result)
    print(json.dumps({'state': result['state'], 'datasets': len(result['datasets']),
                      'mapped_pairs': result['supported_pairs'], 'transfers': len(result['transfers']),
                      'summary': str(directory / 'summary.json')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'dataset', 'transfer', 'export', 'consumer', 'report'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--dataset', choices=DATASETS, default='cal500-raw-activity')
    parser.add_argument('--independent-execution', action='store_true',
                        help='Compatibility option; research always runs independently of pytest')
    args = parser.parse_args()
    directory = inside(args.directory)
    # A separate sibling lock prevents concurrent mutation of this experiment.
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / (directory.name + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit('This mapped-vibe round is already running') from error
        with threadpool_limits(limits=1):
            if args.action == 'freeze':
                freeze(directory)
                return
            validate(directory)
            try:
                if args.action == 'dataset':
                    stage(directory, 'dataset-' + args.dataset, lambda: dataset_stage(directory, args.dataset))
                if args.action == 'run':
                    for dataset in DATASETS:
                        stage(directory, 'dataset-' + dataset, lambda dataset=dataset: dataset_stage(directory, dataset))
                if args.action in ('run', 'transfer'):
                    for source, target in TRANSFER:
                        stage(directory, f'transfer-{source}__{target}', lambda s=source, t=target: transfer_stage(directory, s, t))
                if args.action in ('run', 'export'):
                    stage(directory, 'export', lambda: export_stage(directory))
                if args.action in ('run', 'consumer'):
                    from ..replay.mapped_vibe_consumer import consumer_stage
                    stage(directory, 'consumer', lambda: consumer_stage(directory))
                final_report(directory)
                save(directory / 'status.json', {'state': 'idle', 'active': None, 'updated_at': stamp()})
            except BaseException as error:
                save(directory / 'failure.json', {'error': repr(error), 'at': stamp(), 'pid': os.getpid()})
                raise


if __name__ == '__main__':
    main()
