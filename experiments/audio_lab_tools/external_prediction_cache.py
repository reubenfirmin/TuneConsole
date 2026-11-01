"""Content-addressed cross-fitted predictions for repeated mixture comparisons."""
import hashlib
import json
from pathlib import Path

import numpy as np

from .external_experiment import OUT, digest, save


def array_digest(value):
    array = np.ascontiguousarray(value, dtype='<f8')
    return hashlib.sha256(str(array.shape).encode() + array.tobytes()).hexdigest()


def crossfit(kind, features, y, groups, compute, configuration=None):
    provenance = {'kind': kind, 'configuration': configuration,
        'feature_order': list(features),
        'features': {name: array_digest(value) for name, value in features.items()},
        'targets': array_digest(y), 'groups': list(groups),
        'code': {path.name: digest(path) for path in sorted(Path(__file__).parent.glob('external_*.py'))}}
    key = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    path = OUT / 'crossfit-cache' / (key + '.json')
    if path.exists():
        entry = json.loads(path.read_text())
        values = np.asarray(entry['predictions'])
        if entry['provenance'] != provenance or array_digest(values) != entry['prediction_sha256']:
            raise ValueError('Cross-fitted prediction cache integrity mismatch')
        if not np.isfinite(values).all() or values.shape != (len(y), len(features), y.shape[1]):
            raise ValueError('Invalid cached cross-fitted predictions')
    else:
        values = np.asarray(compute())
        if not np.isfinite(values).all() or values.shape != (len(y), len(features), y.shape[1]):
            raise ValueError('Invalid cross-fitted prediction shape or values')
        save(path, {'provenance': provenance, 'prediction_sha256': array_digest(values),
                    'predictions': values.tolist()})
    return values
