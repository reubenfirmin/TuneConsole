"""Independently replay exported expert pipelines without training or DB reads."""
import json

import numpy as np
from scipy.special import expit
from threadpoolctl import threadpool_limits

from .external_experiment import OUT, digest, save, stamp
from .external_transfer import SNAPSHOT, VIBES


def arrays(model):
    return {key: np.asarray(value) if isinstance(value, list) else value for key, value in model.items()}


def replay(model, x):
    """Inference written independently of the fitting modules."""
    m = arrays(model)
    if x.ndim != 2 or x.shape[1] != len(m['mean']):
        raise ValueError('Exported input dimension mismatch')
    if not (np.asarray(m['scale']) > 0).all():
        raise ValueError('Invalid exported feature scaling')
    z = (np.where(np.isfinite(x), x, m['medians']) - m['mean']) / m['scale']
    kind = m.get('kind', 'linear')
    if kind in ('linear', 'independent_binary', 'softmax'):
        values = z @ m['weights'] + m['intercept']
        if kind == 'independent_binary':
            return expit(values)
        if kind == 'softmax':
            values = np.exp(values - values.max(1, keepdims=True))
            return values / values.sum(1, keepdims=True)
    else:
        reference = m['training_features']
        squared = np.maximum((z * z).sum(1)[:, None] + (reference * reference).sum(1) - 2 * z @ reference.T, 0)
        if kind == 'rbf':
            kernel = np.exp(-m['gamma'] * squared / z.shape[1])
            centered = kernel - kernel.mean(1, keepdims=True) - m['kernel_column_mean'] + m['kernel_grand_mean']
            values = m['intercept'] + centered @ m['dual_weights']
        elif kind == 'neighbours':
            indices = np.argsort(squared, axis=1, kind='stable')[:, :min(m['count'], len(reference))]
            weights = (np.ones_like(indices, dtype=float) if m['weighting'] == 'uniform' else
                       1 / np.maximum(np.sqrt(np.take_along_axis(squared, indices, 1)), 1e-8))
            weights /= weights.sum(1, keepdims=True)
            values = np.sum(weights[:, :, None] * m['training_targets'][indices], axis=1)
        else:
            raise ValueError('Unsupported exported kind: ' + kind)
    return np.clip(values, 0, 1)


def library(model, x):
    model = arrays(model)
    if model.get('kind') == 'independent_binary':
        from .external_binary_calibration import predict
    elif model.get('kind') == 'softmax':
        from .external_calibration import probabilities as predict
    elif model.get('kind'):
        from .external_learners import prediction as predict
    else:
        from .external_training import predict
    return predict(model, x)


def check(path, embedding):
    artifact = json.loads(path.read_text())
    if artifact['vibes'] != list(VIBES):
        raise ValueError('Export changes the six vibe identities/order')
    names = artifact['expert_names']
    features = {'embedding': embedding}
    maximum_error = 0.
    for source, head in artifact['native_heads'].items():
        values = replay(head['model'], embedding)
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite native-head replay')
        maximum_error = max(maximum_error, float(np.max(np.abs(values - library(head['model'], embedding)))))
        features[source] = values[:, head['selected_columns']]
    experts = []
    binary = artifact['format'] == 'external-research-vibe-ranking-1'
    for name in names:
        model, x = artifact['calibrators'][name], features[name]
        values = replay(model, x)
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite calibrator replay')
        maximum_error = max(maximum_error, float(np.max(np.abs(values - library(model, x)))))
        if not binary:
            values = np.maximum(values, 1e-8)
            values /= values.sum(1, keepdims=True)
        experts.append(values)
    stack = np.stack(experts, axis=1)
    expected = 2 * (2 ** len(names) - 1) - len(names)
    if len(artifact['combinations']) != expected:
        raise ValueError('Incomplete exported expert-subset/pooling search')
    subsets = set()
    for candidate in artifact['combinations'].values():
        indices, weights = candidate['indices'], np.array(candidate['weights'])
        if indices != sorted(set(indices)):
            raise ValueError('Invalid expert-subset ordering or duplicate members')
        subsets.add(tuple(indices))
        if candidate['members'] != [names[i] for i in indices]:
            raise ValueError('Exported expert names and indices disagree')
        if weights.shape != ((len(indices), len(VIBES)) if binary else (len(indices),)):
            raise ValueError('Invalid exported pooling shape')
        if (weights < 0).any() or not np.allclose(weights.sum(0), 1, atol=1e-8):
            raise ValueError('Invalid exported pooling simplex')
    if len(subsets) != 2 ** len(names) - 1:
        raise ValueError('Export omits a nonempty expert subset')
    selected = {}
    for policy in ('selected_candidate', 'selected_consensus' if binary else 'selected_combination'):
        candidate = artifact['combinations'][artifact[policy]]
        indices, weights = candidate['indices'], np.array(candidate['weights'])
        pooled = sum(stack[:, index] * weights[i] for i, index in enumerate(indices))
        if pooled.shape != (len(embedding), len(VIBES)) or not np.isfinite(pooled).all() or (pooled < 0).any() or (pooled > 1 + 1e-10).any():
            raise ValueError('Invalid exported pipeline outputs')
        selected[policy] = {'candidate': artifact[policy], 'minimum_score': float(pooled.min()), 'maximum_score': float(pooled.max())}
    if maximum_error > 1e-10:
        raise ValueError('Independent export replay differs from research inference')
    report_path = OUT / (path.stem + '.report.json')
    if artifact['report_sha256'] != digest(report_path):
        raise ValueError('Export does not match its current evaluation report')
    return {'model': path.name, 'sha256': digest(path), 'rows_replayed': len(embedding),
            'experts': len(names), 'nonempty_subsets': len(subsets), 'pooling_candidates': expected,
            'maximum_absolute_replay_error': maximum_error, 'selected': selected}


def evaluate():
    rows = [row for row in json.loads(SNAPSHOT.read_text())['gold'] if len(row.get('embedding_values', [])) == 200]
    embedding = np.array([row['embedding_values'] for row in rows])
    results = []
    with threadpool_limits(limits=1):
        for path in sorted((OUT / 'models').glob('*.json')):
            if not path.name.startswith(('consensus-', 'vibe-ranking-')):
                continue
            results.append(check(path, embedding))
    report = {'created_at': stamp(), 'models': results, 'script_sha256': digest(__file__),
              'validation': 'Deserialize complete exports and independently replay every native head and calibrator on actual saved embeddings. Validate every subset, member index and simplex; replay both selected complete pipelines.',
              'limitations': ['This checks export completeness and numerical inference, not predictive accuracy or product integration. Replayed inputs were also used for final calibration; no training-set quality is reported.']}
    save(OUT / 'export-replay.report.json', report)
    print(json.dumps({'exports_replayed': len(results), 'maximum_error': max((r['maximum_absolute_replay_error'] for r in results), default=None)}), flush=True)
    return report


if __name__ == '__main__':
    evaluate()
