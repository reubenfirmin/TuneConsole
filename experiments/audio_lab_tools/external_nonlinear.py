"""Native nonlinear emotion experts, with identifiable gold overlaps excluded."""
import json
import hashlib
from pathlib import Path

import numpy as np

from .external_experiment import OUT, digest, save
from .external_learners import candidates, prediction, select, serializable
from .external_training import feature_matrices
from .external_transfer import external_head_features


def head_features(datasets, gold, kind='rbf'):
    # Reuse the established representation identity/weight verification.
    _, _, exclusions, signatures, _ = external_head_features(datasets, gold)
    embedding = np.array([row['embedding_values'] for row in gold])
    parts, names, heads = [], [], {}
    for dataset in datasets:
        path = OUT / f'{dataset}.features.json'
        excluded = set(exclusions[dataset])
        rows = [row for row in json.loads(path.read_text())['rows'] if row['id'] not in excluded]
        target_names = rows[0]['target_names']
        columns = [i for i, name in enumerate(target_names) if name.startswith(('Emotion-', 'Usage-'))]
        columns = columns or list(range(len(target_names)))
        y = np.array([row['targets'] for row in rows])[:, columns]
        x = feature_matrices(rows)[0]['embedding']
        selected_names = [target_names[i] for i in columns]
        provenance = {'dataset': dataset, 'kind': kind, 'source_features_sha256': digest(path),
                      'excluded_ids': sorted(excluded), 'target_names': selected_names,
                      'training_ids': [row['id'] for row in rows],
                      'learner_sha256': digest(Path(__file__).with_name('external_learners.py')),
                      'training_sha256': digest(Path(__file__).with_name('external_training.py')),
                      'head_builder_sha256': digest(__file__)}
        key = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
        cache_path = OUT / 'native-head-cache' / (key + '.json')
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            if cached['provenance'] != provenance:
                raise ValueError('Native nonlinear head cache provenance mismatch')
            choice = cached['parameter_key']
            fitted = {key: np.asarray(value) if isinstance(value, list) else value
                      for key, value in cached['model'].items()}
        else:
            choice = select(x, y, [row['group'] for row in rows], False)[kind]
            fitted = candidates(x, y)[choice]
            save(cache_path, {'provenance': provenance, 'parameter_key': choice,
                              'model': serializable(fitted)})
        parts.append(prediction(fitted, embedding))
        names.extend(dataset + ':' + name for name in selected_names)
        heads[dataset] = {'target_names': selected_names, 'selected_columns': list(range(len(columns))),
                          'source_features_sha256': digest(path), 'excluded_ids': sorted(excluded),
                          'kind': kind, 'parameter_key': choice, 'model': serializable(fitted)}
    return np.column_stack(parts), names, exclusions, signatures, heads
