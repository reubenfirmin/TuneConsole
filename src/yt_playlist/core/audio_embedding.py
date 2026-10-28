"""Bounded frozen MusiCNN features, with provenance independent of annotations."""
import hashlib
import json
import math
from statistics import mean

VERSION = 'musicnn-embedding-1'
LAYER = 'model/dense/BiasAdd'
GRAPH_POLICY = 'restore-fused-preactivation-1'
POOLING = 'mean_full_patches'
DIMENSIONS = 200
FEATURES = tuple(f'embedding.{i}' for i in range(DIMENSIONS))
ARTIFACTS = ('musicnn/model.json', 'musicnn/group1-shard1of1.bin')


def validate(value):
    if (not isinstance(value, dict) or set(value) != {
            'version', 'layer', 'graph_policy', 'pooling', 'patches', 'values'}
            or value['version'] != VERSION or value['layer'] != LAYER
            or value['graph_policy'] != GRAPH_POLICY or value['pooling'] != POOLING
            or type(value['patches']) is not int or not 1 <= value['patches'] <= 8
            or not isinstance(value['values'], list) or len(value['values']) != DIMENSIONS
            or any(type(n) not in (int, float) or not math.isfinite(n) or abs(n) > 1e6
                   for n in value['values'])):
        raise ValueError('Invalid frozen music embedding')
    return value


def signature(sample):
    value = validate(sample.get('embedding'))
    hashes = sample.get('artifact_hashes') or {}
    if (sample.get('model') != 'msd-musicnn-1'
            or not isinstance(sample.get('model_version'), str) or not sample['model_version']
            or any(not isinstance(hashes.get(name), str) or len(hashes[name]) != 64
                   or any(c not in '0123456789abcdef' for c in hashes[name]) for name in ARTIFACTS)):
        raise ValueError('Missing frozen music embedding provenance')
    provenance = {key: value[key] for key in ('version', 'layer', 'graph_policy', 'pooling')}
    provenance.update(model=sample['model'], model_version=sample['model_version'],
                      artifacts={name: hashes[name] for name in ARTIFACTS})
    return hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest()


def vector(samples):
    """Equal excerpt means; original excerpt vectors and positions stay captured."""
    if not samples or len({signature(sample) for sample in samples}) != 1:
        raise ValueError('Missing or incompatible frozen music embeddings')
    return [mean(sample['embedding']['values'][i] for sample in samples) for i in range(DIMENSIONS)]
