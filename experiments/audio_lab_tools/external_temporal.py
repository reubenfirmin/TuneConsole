"""Compare static and time-aligned emotion supervision on identical held-out audio."""
from collections import Counter
import csv
import json
from pathlib import Path
import re
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from .external_experiment import DATA, OUT, digest, save, stamp
from .external_training import (choose_alpha, feature_matrices, fit_path, folds,
                                metrics, predict)


def dynamic_annotations(dataset):
    if dataset == 'deam':
        base = DATA / 'deam/annotations/annotations averaged per song/dynamic (per second annotations)'
        result, hashes = {}, {}
        for name in ('arousal', 'valence'):
            path = base / (name + '.csv')
            with path.open() as stream:
                reader = csv.reader(stream)
                header = next(reader)
                times = np.array([int(re.fullmatch(r'sample_(\d+)ms', key).group(1)) / 1000 for key in header[1:]])
                values = {}
                for fields in reader:
                    observations = np.array([float(value) if value.strip() else np.nan for value in fields[1:]])
                    finite = np.isfinite(observations)
                    if finite.any() and np.abs(observations[finite]).max() > 1:
                        raise ValueError('DEAM dynamic labels outside native [-1,1] scale')
                    values[str(int(fields[0]))] = (times[:len(observations)], (observations + 1) / 2)
            result[name] = values
            hashes[path.name] = digest(path)
        return result, hashes
    if dataset != 'pmemo':
        raise ValueError(dataset)
    archive = DATA / 'pmemo/PMEmo.zip'
    result = {}
    with zipfile.ZipFile(archive) as source:
        for name, key in (('arousal', 'A'), ('valence', 'V')):
            values = {}
            with source.open(f'PMEmo/{key}_dynamic_mean.csv') as incoming:
                import io
                reader = csv.reader(io.TextIOWrapper(incoming, encoding='utf-8-sig'))
                next(reader)
                for fields in reader:
                    observations = np.array([float(value) if value.strip() else np.nan for value in fields[1:]])
                    finite = np.isfinite(observations)
                    if (observations[finite] < 0).any() or (observations[finite] > 1).any():
                        raise ValueError('PMEmo dynamic labels outside native [0,1] scale')
                    # The release README explicitly removes the first 15 seconds.
                    values[str(int(fields[0]))] = (15 + .5 * np.arange(len(observations)), observations)
            result[name] = values
    return result, {'archive_sha256': digest(archive)}


def prepare(dataset):
    path = OUT / f'{dataset}.features.json'
    source = json.loads(path.read_text())
    dynamic, hashes = dynamic_annotations(dataset)
    matched, excluded = [], []
    for original in source['rows']:
        identifier = original['id'].split(':')[-1]
        start, end = original['sample_start'], original['sample_start'] + original['sample_seconds']
        targets, counts = [], []
        for name in original['target_names']:
            if identifier not in dynamic[name]:
                break
            times, values = dynamic[name][identifier]
            # Retain only windows fully covered by available annotations.
            if not len(times) or start < times[0] or end > times[-1] + .5:
                break
            selected = (times >= start) & (times < end)
            if selected.sum() < 19 or not np.isfinite(values[selected]).all():
                break
            targets.append(float(values[selected].mean()))
            counts.append(int(selected.sum()))
        if len(targets) != len(original['target_names']):
            excluded.append(original['id'])
            continue
        matched.append({**original, 'static_targets': original['targets'], 'targets': targets,
                        'dynamic_samples_per_target': counts, 'native_scale': [-1, 1] if dataset == 'deam' else [0, 1],
                        'annotation_unit': 'dynamic_listener_mean_inside_actual_center_audio_window'})
    if len(matched) < 30:
        raise ValueError('Too few fully annotated temporal windows')
    alias = dataset + '-dynamic'
    save(OUT / f'{alias}.features.json', {**source, 'rows': matched,
         'temporal_provenance': {'source_features_sha256': digest(path), 'annotation_hashes': hashes}})
    save(OUT / f'{alias}.join-audit.json', {'created_at': stamp(), 'source_rows': len(source['rows']),
         'matched_rows': len(matched), 'excluded_ids': excluded,
         'annotation_hashes': hashes, 'policy': 'Exact numeric recording identifier; average only dynamic labels inside the actual ten-second extracted window, with complete temporal coverage.',
         'pmemo_offset_seconds': 15 if dataset == 'pmemo' else None,
         'dynamic_sample_counts': dict(Counter(count for row in matched for count in row['dynamic_samples_per_target']))})
    return matched


def evaluate(dataset):
    from .external_training import evaluate as native_evaluate
    from .external_learners import evaluate as learners
    with threadpool_limits(limits=1):
        rows = prepare(dataset)
        names = rows[0]['target_names']
        y = np.array([row['targets'] for row in rows])
        static = np.array([row['static_targets'] for row in rows])
        groups = [row['group'] for row in rows]
        split = folds(groups)
        matrices = feature_matrices(rows)[0]
        result, saved_predictions = {}, {}
        for family in ('attributes', 'embedding', 'attributes_embedding'):
            x = matrices[family]
            predictions = {}
            for supervision, labels in (('static', static), ('aligned_dynamic', y)):
                values = np.zeros_like(y)
                for fold in range(5):
                    train, valid = split != fold, split == fold
                    alpha = choose_alpha(x[train], labels[train], np.array(groups)[train].tolist(), list(range(len(names))))
                    values[valid] = predict(fit_path(x[train], labels[train], [alpha])[0], x[valid])
                predictions[supervision] = values
            result[family] = {name: metrics(y, value, names) for name, value in predictions.items()}
            saved_predictions[family] = {name: value.tolist() for name, value in predictions.items()}
        save(OUT / f'{dataset}.temporal-comparison.json', {'created_at': stamp(), 'dataset': dataset,
             'rows': len(rows), 'groups': len(set(groups)), 'target_names': names, 'methods': result,
             'validation': 'Same five grouped outer folds and three inner folds; both methods scored against identical held-out aligned dynamic targets.',
             'source_features_sha256': digest(OUT / f'{dataset}-dynamic.features.json'),
             'script_sha256': digest(__file__), 'predictions': saved_predictions,
             'ids': [row['id'] for row in rows], 'observed_dynamic': y.tolist(),
             'limitations': ['Listener response lag is not separately estimated or optimized.',
                             'The retained subset may differ from the full static corpus; compare the two methods here, not absolute scores from separate target definitions.']})
        native_evaluate(dataset + '-dynamic')
        learners(dataset + '-dynamic')
        print(json.dumps({'temporal_comparison': dataset,
                         'mae': {family: {name: value['mae'] for name, value in methods.items()}
                                 for family, methods in result.items()}}), flush=True)
        return result
