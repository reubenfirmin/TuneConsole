"""Freeze and run independent per-target/per-vibe specialists under experiments/."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .. import TOOLS
from ..training.external_binary_calibration import metrics as vibe_metrics
from ..support.external_experiment import LAB, OUT, digest, save, stamp
from ..evaluation.external_ranking import average_precision
from ..training.external_training import (bootstrap_improvement, fit_path, folds, metrics,
                                        predict as ridge_predict, validation_groups)
from ..evaluation.external_transfer import (SNAPSHOT, VIBES, excluded_identities, probabilities,
                                          select_vibe_alpha)
from . import vibe_specialist_models as learner

EXPERIMENTS = LAB.parent
DEFAULT = LAB / 'local/per-vibe-20261029/round-1'
RESERVED_PLAN = OUT / 'checkpoint-1/reserved-cohort/plan.json'
HUMANS = OUT / 'checkpoint-1/inputs/human-labels.json'
NATIVE = ('emotify-artist', 'cal500-raw-activity', 'pmemo', 'deam')
SELECTORS = ('incumbent', 'shared_ap', 'independent_ap', 'independent_loss', 'guarded_ap')


def inside(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(EXPERIMENTS.resolve()):
        raise ValueError('Round paths must stay inside experiments/: ' + str(path))
    return resolved


def load(path, expected=None):
    path = inside(path)
    if expected is not None and digest(path) != expected:
        raise ValueError('Input checksum changed: ' + str(path))
    return json.loads(path.read_text())


def versions():
    return {'python': sys.version, **{name: importlib.metadata.version(name)
            for name in ('numpy', 'scipy', 'threadpoolctl')}}


def sources():
    directory = Path(__file__).parent
    paths = [directory / 'vibe_specialist_models.py', Path(__file__),
             directory / 'test_vibe_specialists.py',
             EXPERIMENTS / 'run.py']
    paths += sorted(TOOLS.rglob('external_*.py'))
    paths += sorted(TOOLS.rglob('__init__.py'))
    return {str(path.relative_to(EXPERIMENTS)): digest(path) for path in paths}


def npz(path, **arrays):
    path = inside(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.writing.npz')
    np.savez_compressed(temporary, **arrays)
    temporary.replace(path)


def input_arrays(path):
    with np.load(inside(path), allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def freeze(directory):
    directory = inside(directory)
    if (directory / 'plan.json').exists():
        return validate(directory)
    if directory.exists() and list(directory.iterdir()):
        raise ValueError('Incomplete/nonempty round directory; preserve it and choose a new --directory')
    directory.mkdir(parents=True, exist_ok=True)
    legacy = load(RESERVED_PLAN)
    historical = load(SNAPSHOT, legacy['historical_snapshot_sha256'])
    gold = [row for row in historical['gold'] if len(row.get('embedding_values', [])) == 200]
    reserved_ids = set(legacy['reserved_ids'])
    if any(row['video_id'] in reserved_ids for row in gold):
        raise ValueError('Reserved additions leaked into historical development inputs')
    signature = legacy['embedding_signature']
    if any(row['embedding_signature'] != signature for row in gold):
        raise ValueError('Incompatible local embedding signature')
    # Read identities, discard all labels and vectors from the additions. They
    # protect native fits but never become outcomes or scoring inputs here.
    protected = [{key: row.get(key, '') for key in ('video_id', 'artist', 'title')}
                 for row in load(HUMANS, legacy['snapshot_sha256'])['gold']]
    embeddings = np.asarray([row['embedding_values'] for row in gold], dtype=float)
    y = np.array([[int(vibe in row['labels']) for vibe in VIBES] for row in gold], dtype=float)
    if not np.isfinite(embeddings).all() or not (y.sum(1) > 0).all():
        raise ValueError('Invalid historical development vectors or labels')
    local_rows = [{**{k: row.get(k, '') for k in ('artist', 'title', 'video_id')},
                   'id': row['video_id'], 'group': row['video_id']} for row in gold]
    local_groups = validation_groups(local_rows)
    local_meta = {'rows': local_rows, 'groups': local_groups, 'vibes': list(VIBES),
                  'protected_identities': protected,
                  'excluded_reserved_ids': sorted(reserved_ids),
                  'snapshot_sha256': digest(SNAPSHOT), 'human_snapshot_sha256': digest(HUMANS)}
    save(directory / 'inputs/local.json', local_meta)
    npz(directory / 'inputs/local.npz', embedding=embeddings, y=y,
        outer=folds(local_groups, seed=learner.SEED))
    checkpoint_record = legacy['candidates']['checkpoint']
    checkpoint = load(checkpoint_record['path'], checkpoint_record['sha256'])
    native = checkpoint['native']
    # Deliberately discard its full-development six-vibe calibration.
    incumbent_native = {'medians': native['mean'], **native}
    save(directory / 'inputs/incumbent-native.json', incumbent_native)
    expanded_record = legacy['candidates']['expanded_rankings']
    expanded = load(expanded_record['path'], expanded_record['sha256'])
    heads = expanded['native_heads']
    save(directory / 'inputs/frozen-heads.json', heads)
    references = {str(RESERVED_PLAN): digest(RESERVED_PLAN),
                  checkpoint_record['path']: checkpoint_record['sha256'],
                  expanded_record['path']: expanded_record['sha256'],
                  str(SNAPSHOT): digest(SNAPSHOT), str(HUMANS): digest(HUMANS)}
    native_counts = {}
    from yt_playlist.core.audio_embedding import signature as embedding_signature
    for dataset in NATIVE:
        index_path = OUT / f'{dataset}.features.json'
        index = load(index_path, heads[dataset]['source_features_sha256'])
        rejected = excluded_identities(index['rows'], protected)
        rejected_ids = {row['id'] for row in rejected}
        rows = [row for row in index['rows'] if row['id'] not in rejected_ids]
        all_names = rows[0]['target_names']
        columns = [i for i, name in enumerate(all_names) if name.startswith(('Emotion-', 'Usage-'))]
        columns = columns or list(range(len(all_names)))
        feature_hashes, vectors = {}, []
        for row in rows:
            if row['target_names'] != all_names:
                raise ValueError('Native target columns differ: ' + dataset)
            feature_path = inside(row['feature_file'])
            feature = load(feature_path)['prediction']
            actual_signature = embedding_signature({**feature, 'artifact_hashes': {
                item['path']: item['sha256'] for item in feature['bundle']['files']}})
            if actual_signature != signature:
                raise ValueError('Native embedding signature mismatch: ' + row['id'])
            vectors.append(feature['embedding']['values'])
            feature_hashes[str(feature_path)] = digest(feature_path)
        x = np.array(vectors, dtype=float)
        target = np.array([row['targets'] for row in rows], dtype=float)[:, columns]
        if x.shape != (len(rows), 200) or not np.isfinite(x).all() or not np.isfinite(target).all():
            raise ValueError('Invalid native arrays: ' + dataset)
        if target.min() < 0 or target.max() > 1:
            raise ValueError('Native annotations outside normalized scale: ' + dataset)
        groups = validation_groups(rows)
        metadata = {'dataset': dataset, 'target_names': [all_names[i] for i in columns],
                    'groups': groups, 'rows': [{k: row.get(k) for k in (
                        'id', 'group', 'artist', 'title', 'audio_sha256', 'annotation_unit',
                        'sample_start', 'sample_seconds', 'rater_count')} for row in rows],
                    'excluded_ids': sorted(rejected_ids), 'source_sha256': digest(index_path),
                    'feature_files': feature_hashes, 'extraction_errors': index.get('errors', [])}
        save(directory / f'inputs/{dataset}.json', metadata)
        npz(directory / f'inputs/{dataset}.npz', x=x, y=target,
            outer=folds(groups, seed=learner.SEED))
        references[str(index_path)] = digest(index_path)
        native_counts[dataset] = {'rows': len(rows), 'groups': len(set(groups)),
                                  'targets': len(columns), 'excluded': len(rejected)}
    # Retained native experts are provenance-checked frozen representations.
    for dataset, head in heads.items():
        path = OUT / f'{dataset}.features.json'
        source = load(path, head['source_features_sha256'])
        expected_exclusions = {row['id'] for row in excluded_identities(source['rows'], protected)}
        if not expected_exclusions.issubset(set(head['excluded_ids'])):
            raise ValueError('Frozen expert did not exclude protected identities: ' + dataset)
        references[str(path)] = head['source_features_sha256']
    archive = directory / 'source'
    for relative, expected in sources().items():
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXPERIMENTS / relative, destination)
        if digest(destination) != expected:
            raise ValueError('Research source changed while freezing')
    inputs = {str(path.relative_to(directory)): digest(path)
              for path in sorted((directory / 'inputs').iterdir())}
    plan = {'format': 'per-vibe-specialists-v1', 'created_at': stamp(),
            'native': native_counts, 'local_rows': len(gold), 'local_groups': len(set(local_groups)),
            'protected_identities': len(protected), 'reserved_additions_excluded': len(reserved_ids),
            'embedding_signature': signature, 'inputs': inputs, 'references': references,
            'code': sources(), 'versions': versions(), 'native_grid': learner.grid(),
            'local_grid': learner.grid(binary=True), 'selectors': list(SELECTORS),
            'local_sources': ['embedding', 'incumbent-native', *heads,
                              *['specialist:' + source for source in NATIVE]],
            'seed': learner.SEED, 'outer_folds': 5, 'inner_folds': 3,
            'ap_guard': {'minimum_gain': .02, 'minimum_inner_fold_wins': 2},
            'local_status': 'exploratory reused development labels; no new independent confirmation',
            'product_adoption': False}
    save(directory / 'plan.json', plan)
    print(json.dumps({'plan': str(directory / 'plan.json'), 'native': native_counts,
                      'local_rows': len(gold), 'protected_identities': len(protected)}), flush=True)
    return plan


def validate(directory):
    plan = load(directory / 'plan.json')
    if plan['code'] != sources() or plan['versions'] != versions():
        raise ValueError('Frozen research code or numerical runtime changed; use a new round')
    for relative, expected in plan['inputs'].items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory.resolve()) or digest(path) != expected:
            raise ValueError('Frozen input changed: ' + relative)
    for relative, expected in plan['code'].items():
        if digest(inside(directory / 'source' / relative)) != expected:
            raise ValueError('Archived research source changed: ' + relative)
    return plan


def receipt_valid(directory, name, fingerprint):
    path = directory / f'{name}.receipt.json'
    if not path.exists():
        return False
    receipt = load(path)
    if receipt['fingerprint'] != fingerprint or receipt['state'] != 'complete':
        return False
    for relative, expected in receipt['outputs'].items():
        output = inside(directory / relative)
        if not output.is_relative_to(directory.resolve()) or not output.exists() or digest(output) != expected:
            raise ValueError('Completed stage output changed: ' + relative)
    return True


def stage(directory, name, function):
    fingerprint = digest(directory / 'plan.json')
    if receipt_valid(directory, name, fingerprint):
        print(json.dumps({'stage': name, 'state': 'cached_verified'}), flush=True)
        return
    save(directory / 'status.json', {'state': 'running', 'active': name, 'pid': os.getpid(), 'updated_at': stamp()})
    start = time.perf_counter()
    outputs = function()
    save(directory / f'{name}.receipt.json', {'state': 'complete', 'fingerprint': fingerprint,
         'finished_at': stamp(), 'seconds': time.perf_counter() - start,
         'outputs': {str(path.relative_to(directory)): digest(path) for path in outputs}})
    print(json.dumps({'stage': name, 'state': 'complete', 'seconds': time.perf_counter() - start}), flush=True)


def candidate_oof(x, y, groups, specs):
    split = folds(groups, count=3, seed=learner.SEED + 1)
    predictions = np.zeros((len(y), len(specs), y.shape[1]))
    for fold in range(3):
        train, valid = split != fold, split == fold
        for i, model in enumerate(learner.fit_models(x[train], y[train], specs)):
            predictions[valid, i] = learner.predict(model, x[valid])
    return predictions, split


def fit_selected(x, y, specs, selected):
    ids = sorted(set(selected))
    fitted = learner.fit_models(x, y, [specs[i] for i in ids])
    models = dict(zip(ids, fitted))
    return [learner.column_model(models[i], j) for j, i in enumerate(selected)]


def native_stage(directory, dataset):
    metadata = load(directory / f'inputs/{dataset}.json')
    arrays = input_arrays(directory / f'inputs/{dataset}.npz')
    x, y, outer = arrays['x'], arrays['y'], arrays['outer']
    groups, specs = np.array(metadata['groups']), learner.grid()
    predictions = {key: np.zeros_like(y) for key in ('shared', 'independent', 'training_median')}
    choices = []
    for fold in range(5):
        train, valid = outer != fold, outer == fold
        inner, _ = candidate_oof(x[train], y[train], groups[train], specs)
        selected = learner.select_native(y[train], inner, specs)
        choices.append({'fold': fold, **selected})
        for selector in ('shared', 'independent'):
            heads = fit_selected(x[train], y[train], specs, selected[selector])
            predictions[selector][valid] = np.column_stack([learner.predict(m, x[valid])[:, 0] for m in heads])
        predictions['training_median'][valid] = np.median(y[train], axis=0)
        print(json.dumps({'dataset': dataset, 'outer_fold': fold, 'state': 'complete'}), flush=True)
    inner, _ = candidate_oof(x, y, groups, specs)
    final = learner.select_native(y, inner, specs)
    heads = fit_selected(x, y, specs, final['independent'])
    serialized = learner.serial(heads)
    maximum = 0.
    for live, exported in zip(heads, serialized):
        maximum = max(maximum, float(np.max(np.abs(learner.predict(live, x) - learner.replay(exported, x)))))
    if maximum > 1e-10:
        raise ValueError('Native exported replay mismatch')
    report = {'dataset': dataset, 'rows': len(y), 'groups': len(set(groups)),
              'target_names': metadata['target_names'], 'excluded_ids': metadata['excluded_ids'],
              'metrics': {key: metrics(y, p, metadata['target_names']) for key, p in predictions.items()},
              'paired_fixed_oof': bootstrap_improvement(y, predictions['shared'], predictions['independent'],
                                                       groups, list(range(y.shape[1]))),
              'fold_selection': choices, 'full_development_selection': final,
              'independent_export_max_absolute_error': maximum,
              'native_task_only': 'Human emotion/activity annotations; not six-vibe validation'}
    prefix = directory / f'native-{dataset}'
    save(prefix.with_suffix('.report.json'), report)
    save(prefix.with_suffix('.model.json'), {'heads': serialized, 'selection': final,
                                           'target_names': metadata['target_names']})
    npz(prefix.with_suffix('.oof.npz'), **predictions)
    return [prefix.with_suffix(suffix) for suffix in ('.report.json', '.model.json', '.oof.npz')]


def local_features(directory, embedding):
    frozen = load(directory / 'inputs/frozen-heads.json')
    native = load(directory / 'inputs/incumbent-native.json')
    native = {k: np.asarray(v) for k, v in native.items()}
    features = {'embedding': embedding, 'incumbent-native': ridge_predict(native, embedding)}
    for name, head in frozen.items():
        model = {k: np.asarray(v) if isinstance(v, list) else v for k, v in head['model'].items()}
        features[name] = ridge_predict(model, embedding)[:, head['selected_columns']]
    for dataset in NATIVE:
        models = load(directory / f'native-{dataset}.model.json')['heads']
        features['specialist:' + dataset] = np.column_stack([learner.replay(m, embedding)[:, 0] for m in models])
    return features


def incumbent_fit(x, y, groups):
    target = y / y.sum(1, keepdims=True)
    alpha = select_vibe_alpha(x, target, list(groups))
    return {'kind': 'incumbent', 'alpha': alpha, **fit_path(x, target, [alpha])[0]}


def incumbent_predict(model, x):
    arrays = {k: np.asarray(v) if isinstance(v, list) else v for k, v in model.items()}
    return probabilities(ridge_predict(arrays, x))


def local_catalogue(features):
    return [{'source': 'incumbent-native', 'family': 'incumbent'}] + [
        {'source': source, **spec} for source in features for spec in learner.grid(binary=True)]


def local_training_oof(features, y, groups, catalogue):
    split = folds(groups, count=3, seed=learner.SEED + 1)
    values = np.zeros((len(y), len(catalogue), y.shape[1]))
    for fold in range(3):
        train, valid = split != fold, split == fold
        native = features['incumbent-native']
        model = incumbent_fit(native[train], y[train], groups[train])
        values[valid, 0] = incumbent_predict(model, native[valid])
        for source, x in features.items():
            ids = [i for i, spec in enumerate(catalogue) if i and spec['source'] == source]
            models = learner.fit_models(x[train], y[train], [catalogue[i] for i in ids])
            for i, fitted in zip(ids, models):
                values[valid, i] = learner.predict(fitted, x[valid])
    return values, split


def local_selected(features, y, groups, catalogue, selected):
    result = {}
    if 0 in selected:
        result[0] = incumbent_fit(features['incumbent-native'], y, groups)
    for source, x in features.items():
        ids = sorted({i for i in selected if i and catalogue[i]['source'] == source})
        if ids:
            result.update(zip(ids, learner.fit_models(x, y, [catalogue[i] for i in ids])))
    return result


def selected_predictions(models, catalogue, selected, features):
    values = {}
    for i in set(selected):
        x = features[catalogue[i]['source']]
        values[i] = incumbent_predict(models[i], x) if i == 0 else learner.predict(models[i], x)
    return np.column_stack([values[i][:, j] for j, i in enumerate(selected)])


def replay_bundle(directory, export, selector, embedding):
    """Replay from raw saved embeddings using only serialized dependencies."""
    for relative, expected in export['dependency_sha256'].items():
        if digest(inside(directory / relative)) != expected:
            raise ValueError('Export dependency changed: ' + relative)
    records = export['selectors'][selector]
    needed = {record['spec']['source'] for record in records}
    representations = {'embedding': embedding}
    old = load(directory / 'inputs/frozen-heads.json')
    for source in needed - {'embedding'}:
        if source == 'incumbent-native':
            model = load(directory / 'inputs/incumbent-native.json')
            representations[source] = learner.replay({'kind': 'ridge', **model}, embedding)
        elif source.startswith('specialist:'):
            heads = load(directory / f"native-{source.split(':', 1)[1]}.model.json")['heads']
            representations[source] = np.column_stack([learner.replay(m, embedding)[:, 0] for m in heads])
        else:
            model = {'kind': 'ridge', **old[source]['model']}
            representations[source] = learner.replay(model, embedding)[:, old[source]['selected_columns']]
    columns = []
    for record in records:
        x = representations[record['spec']['source']]
        model = record['model']
        if record['spec']['family'] == 'incumbent':
            raw = learner.replay({**model, 'kind': 'ridge'}, x)
            positive = np.maximum(raw, 1e-8)
            values = positive / positive.sum(1, keepdims=True)
        else:
            values = learner.replay(model, x)
        columns.append(values[:, record['column']])
    return np.column_stack(columns)


def ap_intervals(y, baseline, prediction, groups):
    keys = sorted(set(groups))
    indices = [np.flatnonzero(groups == key) for key in keys]
    draws = np.random.default_rng(learner.SEED).integers(len(keys), size=(2000, len(keys)))
    result = {}
    for j, vibe in enumerate(VIBES):
        values = []
        for draw in draws:
            use = np.concatenate([indices[i] for i in draw])
            if len(np.unique(y[use, j] > 0)) < 2:
                continue
            values.append(average_precision(y[use, j] > 0, prediction[use, j]) -
                          average_precision(y[use, j] > 0, baseline[use, j]))
        result[vibe] = {'conditional_95_percent_interval': np.quantile(values, [.025, .975]).tolist()
                        if values else None, 'evaluable_bootstrap_draws': len(values)}
    return {'per_vibe': result,
            'method': 'paired artist-group bootstrap of fixed OOF AP differences; excludes selection uncertainty; no multiplicity correction'}


def local_stage(directory):
    metadata = load(directory / 'inputs/local.json')
    arrays = input_arrays(directory / 'inputs/local.npz')
    y, outer = arrays['y'], arrays['outer']
    features = local_features(directory, arrays['embedding'])
    groups, catalogue = np.array(metadata['groups']), local_catalogue(features)
    predictions = {name: np.zeros_like(y) for name in SELECTORS}
    choices = []
    for fold in range(5):
        train, valid = outer != fold, outer == fold
        training = {name: x[train] for name, x in features.items()}
        testing = {name: x[valid] for name, x in features.items()}
        inner, split = local_training_oof(training, y[train], groups[train], catalogue)
        choice = learner.select_vibes(y[train], inner, split)
        ids = [i for name in SELECTORS for i in choice[name]]
        models = local_selected(training, y[train], groups[train], catalogue, ids)
        for name in SELECTORS:
            predictions[name][valid] = selected_predictions(models, catalogue, choice[name], testing)
        choices.append({'fold': fold, **choice})
        print(json.dumps({'local_outer_fold': fold, 'choices': {
            name: [catalogue[i] for i in choice[name]] for name in SELECTORS}, 'state': 'complete'}), flush=True)
    inner, split = local_training_oof(features, y, groups, catalogue)
    choice = learner.select_vibes(y, inner, split)
    models = local_selected(features, y, groups, catalogue, [i for name in SELECTORS for i in choice[name]])
    exports, replay_error = {}, 0.
    start = time.perf_counter()
    for selector in SELECTORS:
        exports[selector] = []
        for j, i in enumerate(choice[selector]):
            model = models[i] if i == 0 else learner.column_model(models[i], j)
            record = {'vibe': VIBES[j], 'column': j if i == 0 else 0,
                      'candidate_index': i, 'spec': catalogue[i], 'model': learner.serial(model)}
            x = features[catalogue[i]['source']]
            expected = (incumbent_predict(model, x)[:, j] if i == 0 else learner.predict(model, x)[:, 0])
            if i == 0:
                # Independently evaluate all six linear logits before the
                # incumbent's row normalization; retain the selected column.
                raw = learner.replay({**record['model'], 'kind': 'ridge'}, x)
                observed = np.maximum(raw, 1e-8)
                observed = (observed / observed.sum(1, keepdims=True))[:, j]
            else:
                observed = learner.replay(record['model'], x)[:, 0]
            replay_error = max(replay_error, float(np.max(np.abs(expected - observed))))
            exports[selector].append(record)
    if replay_error > 1e-10:
        raise ValueError('Local specialist exported replay mismatch')
    export = directory / 'local.model.json'
    export_data = {'format': 'per-vibe-specialists-research-v1', 'selectors': exports,
                  'plan_sha256': digest(directory / 'plan.json'),
                  'dependency_sha256': {str(path.relative_to(directory)): digest(path) for path in [
                      directory / 'inputs/frozen-heads.json', directory / 'inputs/incumbent-native.json',
                      *[directory / f'native-{source}.model.json' for source in NATIVE]]},
                  'embedding_signature': load(directory / 'plan.json')['embedding_signature'],
                  'research_only': True}
    save(export, export_data)
    timings, bundle_error = {}, 0.
    for selector in SELECTORS:
        expected = selected_predictions(models, catalogue, choice[selector], features)
        before = time.perf_counter()
        observed = replay_bundle(directory, load(export), selector, arrays['embedding'])
        timings[selector] = {'cold_bundle_replay_seconds': time.perf_counter() - before,
                             'songs': len(y), 'includes_dependency_checks_and_json_loading': True}
        bundle_error = max(bundle_error, float(np.max(np.abs(observed - expected))))
    if bundle_error > 1e-10:
        raise ValueError('End-to-end exported bundle replay mismatch')
    report = {'rows': len(y), 'groups': len(set(groups)), 'vibes': list(VIBES),
              'candidate_count': len(catalogue), 'catalogue': catalogue,
              'metrics': {name: vibe_metrics(y, values) for name, values in predictions.items()},
              'paired_fixed_oof': {name: ap_intervals(y, predictions['incumbent'], predictions[name], groups)
                                   for name in SELECTORS if name != 'incumbent'},
              'fold_selection': choices, 'full_development_selection': choice,
              'independent_export_max_absolute_error': replay_error,
              'independent_bundle_max_absolute_error': bundle_error,
              'inference': timings,
              'all_selector_head_replay_seconds': time.perf_counter() - start,
              'model_bytes': export.stat().st_size,
              'complete_research_bundle_bytes': export.stat().st_size + sum(
                  (directory / relative).stat().st_size for relative in export_data['dependency_sha256']),
              'limitations': ['Reused historical developer labels: exploratory, not fresh product validation.',
                             'Reserved additions are excluded from fitting, selection and scoring.',
                             'Outer OOF evaluates the predeclared selection procedure, not the final full-data model.',
                             'Unselected label alternatives are incomplete comparison labels, not explicit negative judgments.',
                             'Class-balanced outputs are not population-calibrated probabilities.',
                             'Native human-target improvement does not establish six-vibe suitability.',
                             'No live model change; new independent listening/reference evidence required.']}
    save(directory / 'local.report.json', report)
    npz(directory / 'local.oof.npz', **predictions)
    return [directory / name for name in ('local.report.json', 'local.oof.npz', 'local.model.json')]


def report(directory):
    plan = load(directory / 'plan.json')
    summary = {'plan_sha256': digest(directory / 'plan.json'), 'native': {}, 'local': None,
               'local_status': plan['local_status'], 'product_adoption': False}
    fingerprint = summary['plan_sha256']
    for dataset in NATIVE:
        if receipt_valid(directory, 'native-' + dataset, fingerprint):
            native = load(directory / f'native-{dataset}.report.json')
            summary['native'][dataset] = {k: native[k] for k in ('rows', 'groups', 'metrics', 'paired_fixed_oof')}
    if receipt_valid(directory, 'local', fingerprint):
        local = load(directory / 'local.report.json')
        summary['local'] = {k: local[k] for k in ('metrics', 'paired_fixed_oof', 'rows', 'groups', 'limitations',
                                                'model_bytes', 'independent_export_max_absolute_error')}
    summary['state'] = 'complete' if len(summary['native']) == 4 and summary['local'] else 'partial'
    save(directory / 'summary.json', summary)
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'run', 'report'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--independent-execution', action='store_true',
                        help='Compatibility option; research always runs independently of pytest')
    args = parser.parse_args()
    directory = inside(args.directory)
    if args.action == 'plan':
        freeze(directory)
        return
    if args.action == 'report':
        report(directory)
        return
    validate(directory)
    # Never admit two writers to one round directory.
    import fcntl
    with (directory / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another invocation owns this round')
        with threadpool_limits(limits=1):
            try:
                for dataset in NATIVE:
                    stage(directory, 'native-' + dataset, lambda dataset=dataset: native_stage(directory, dataset))
                stage(directory, 'local', lambda: local_stage(directory))
                summary = report(directory)
                save(directory / 'status.json', {'state': summary['state'], 'active': None, 'updated_at': stamp()})
            except BaseException as error:
                save(directory / 'status.json', {'state': 'failed', 'error': repr(error), 'updated_at': stamp()})
                raise


if __name__ == '__main__':
    main()
