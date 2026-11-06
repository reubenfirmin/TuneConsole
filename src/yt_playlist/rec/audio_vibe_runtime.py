"""Neutral, bounded inference helpers for frozen audio-vibe specialists.

Specialists own their artifact contracts, native targets and aggregation policy.
This module owns bounded loading, validation, coefficient arithmetic and optional-model isolation.
It never loads annotations, research modules or network resources.
"""
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding, fao, features
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION

MAX_EXCERPTS = 128
MODEL_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError)


def configured(name, default):
    if name in os.environ:
        return os.environ[name]
    try:
        for line in features.LOCAL_ENV.read_text().splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key.strip() == name:
                return value.strip().strip('\"\'')
    except OSError:
        pass
    return default


def identity(version, selected_mode, path, loader):
    if selected_mode != 'off' and path:
        try:
            return '/'.join((version, selected_mode, loader(path)['sha256']))
        except MODEL_ERRORS:
            pass
    return '/'.join((version, selected_mode, 'shared'))


def valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def vector(values, size, positive=False):
    return (isinstance(values, list) and len(values) == size
            and all(finite(v) and (not positive or v > 0) for v in values))


def matrix(values, rows, columns):
    return isinstance(values, list) and len(values) == rows and all(vector(row, columns) for row in values)


def validate_stage(model, inputs, outputs, *, allow_rff=True):
    if not isinstance(model, dict) or any(not vector(model.get(key), inputs, key == 'scale')
                                        for key in ('medians', 'mean', 'scale')):
        raise ValueError('Invalid audio-vibe preprocessing')
    kind = model.get('kind')
    if kind == 'ridge':
        if not matrix(model.get('weights'), inputs, outputs) or not vector(model.get('intercept'), outputs):
            raise ValueError('Invalid audio-vibe ridge coefficients')
    elif kind == 'rff' and allow_rff:
        width = model.get('features')
        if (type(width) is not int or not 1 <= width <= 128
                or not matrix(model.get('frequencies'), inputs, width)
                or not vector(model.get('offsets'), width)
                or not isinstance(model.get('linear'), dict)):
            raise ValueError('Invalid audio-vibe random features')
        validate_stage({'kind': 'ridge', **model.get('linear', {})}, width, outputs, allow_rff=False)
    else:
        raise ValueError('Unsupported audio-vibe learner')


def validate_consensus(artifact, expected, targets):
    """Validate a source-native ridge consensus against its specialist's explicit header."""
    label = expected['vibe'].capitalize()
    if (not isinstance(artifact, dict)
            or any(artifact.get(key) != value for key, value in expected.items())
            or artifact.get('taxonomy_version') != TAXONOMY_VERSION
            or artifact.get('feature_version') != audio_embedding.VERSION
            or artifact.get('feature_names') != list(audio_embedding.FEATURES)
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or any(not valid_hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or not isinstance(artifact.get('experts'), dict) or set(artifact['experts']) != set(targets)):
        raise ValueError(f'Incompatible {label} model')
    for source, names in targets.items():
        expert = artifact['experts'][source]
        if (not isinstance(expert, dict) or expert.get('target_names') != names
                or type(expert.get('training_rows')) is not int or expert['training_rows'] <= 0):
            raise ValueError(f'Invalid {label} source identity')
        validate_stage(expert.get('model'), audio_embedding.DIMENSIONS, len(names), allow_rff=False)
    return artifact


def read_artifact(path, size, max_bytes, unpack, *, exact_size=True):
    """Read logical FAO bytes; callers retain their own two-entry identity cache.

    Cruise historically permits a size change within its byte bound. Other
    specialists require a positive size and an exact-length read.
    """
    if size > max_bytes or (exact_size and size <= 0):
        raise ValueError('Audio-vibe artifact exceeds size bound')
    with fao.open_reader(path, max_bytes=max_bytes) as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes or (exact_size and len(raw) != size):
        raise ValueError('Audio-vibe artifact changed or exceeds size bound')
    try:
        model = unpack(json.loads(raw))
    except RecursionError as error:
        raise ValueError('Invalid audio-vibe artifact nesting') from error
    model['sha256'] = hashlib.sha256(raw).hexdigest()
    return model


def consensus_loader(validate, max_bytes):
    """Give one specialist its own two-entry cache keyed by physical file identity."""
    @lru_cache(maxsize=2)
    def cached(path, file_identity, size):
        return read_artifact(path, size, max_bytes, lambda artifact: {'header': validate(artifact)})

    def load(path):
        path = Path(path).resolve()
        info = fao.stat(path)
        return cached(str(path), info.identity, info.logical_bytes)

    return load


def predict(model, values):
    normalized = [(v - center) / scale for v, center, scale in
                  zip(values, model['mean'], model['scale'], strict=True)]
    if not all(finite(v) for v in normalized):
        raise ValueError('Nonfinite audio-vibe normalized input')
    if model['kind'] == 'rff':
        width = model['features']
        projected = [math.cos(offset + sum(row[i] * value for row, value in
                     zip(model['frequencies'], normalized, strict=True))) * math.sqrt(2 / width)
                     for i, offset in enumerate(model['offsets'])]
        return predict({'kind': 'ridge', **model['linear']}, projected)
    result = [intercept + sum(row[i] * value for row, value in
                             zip(model['weights'], normalized, strict=True))
              for i, intercept in enumerate(model['intercept'])]
    if not all(finite(v) for v in result):
        raise ValueError('Nonfinite audio-vibe native output')
    return [max(0., min(1., value)) for value in result]


def native_attributes(model, targets, values):
    """Predict and clip each excerpt before averaging each source-native target."""
    predicted = [predict(model, row) for row in values]
    return dict(zip(targets, (mean(column) for column in zip(*predicted, strict=True)), strict=True))


def chunks(values):
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_EXCERPTS
            or not all(vector(row, audio_embedding.DIMENSIONS) and all(abs(v) <= 1e6 for v in row)
                       for row in values)):
        raise ValueError('Invalid audio-vibe excerpt embeddings')
    return values


def apply(samples, prediction, *, selected_mode, path, loader, scorer, vibe):
    """Merge one eligible specialist without disturbing earlier specialists."""
    if selected_mode == 'off' or not path:
        return prediction
    try:
        model = loader(path)
        if {audio_embedding.signature(sample) for sample in samples} != {model['header']['embedding_signature']}:
            return prediction
        candidate = scorer(model, [sample['embedding']['values'] for sample in samples])
    except MODEL_ERRORS:
        return prediction
    suitability = {row['vibe']: row['score'] for row in prediction['ranking']}
    suitability.update(prediction.get('suitability', {}))
    if selected_mode == 'active':
        suitability[vibe] = candidate['score']
    return {**prediction, 'suitability': suitability,
            'individual_models': {**prediction.get('individual_models', {}),
                                  vibe: {**candidate, 'mode': selected_mode}}}
