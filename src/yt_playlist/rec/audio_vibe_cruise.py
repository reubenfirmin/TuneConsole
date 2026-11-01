"""Bounded Cruise-only inference, with the shared scorer retained for five vibes.

It reads bundled coefficients, never annotations or experiment files.
Individual suitability is used only within a selected vibe; overall six-way
classification continues to use the shared model's comparable scores.
"""
import base64
import binascii
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean
import zlib

from yt_playlist.core import audio_embedding, features
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_external as shared

FORMAT = 'audio-vibe-cruise-1'
MAX_BYTES = 256 * 1024
MAX_PAYLOAD = 1024 * 1024
MAX_EXCERPTS = 128
SOURCES = ('pmemo', 'deam')
VERSION = 'cruise-routing-1'
DEFAULT_PATH = Path(__file__).with_name('data') / 'cruise-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_CRUISE_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_CRUISE_MODE'


def _configured(name, default):
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


def configured_path():
    return _configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    value = _configured(MODE_KEY, 'active')
    return value if value in ('active', 'shadow', 'off') else 'off'


def identity():
    """Include routing policy and mode in bounded background invalidation."""
    selected_mode, path = mode(), configured_path()
    if selected_mode != 'off' and path:
        try:
            return '/'.join((VERSION, selected_mode, load(path)['sha256']))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
    return '/'.join((VERSION, selected_mode, 'shared'))


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(values, size, positive=False):
    return (isinstance(values, list) and len(values) == size
            and all(_finite(v) and (not positive or v > 0) for v in values))


def _matrix(values, rows, columns):
    return isinstance(values, list) and len(values) == rows and all(_vector(row, columns) for row in values)


def _stage(model, inputs, outputs, *, allow_rff=True):
    if not isinstance(model, dict) or any(not _vector(model.get(key), inputs, key == 'scale')
                                        for key in ('medians', 'mean', 'scale')):
        raise ValueError('Invalid Cruise preprocessing')
    kind = model.get('kind')
    if kind == 'ridge':
        if not _matrix(model.get('weights'), inputs, outputs) or not _vector(model.get('intercept'), outputs):
            raise ValueError('Invalid Cruise ridge coefficients')
    elif kind == 'rff' and allow_rff:
        width = model.get('features')
        if (type(width) is not int or not 1 <= width <= 128
                or not _matrix(model.get('frequencies'), inputs, width)
                or not _vector(model.get('offsets'), width)
                or not isinstance(model.get('linear'), dict)):
            raise ValueError('Invalid Cruise random features')
        _stage({'kind': 'ridge', **model.get('linear', {})}, width, outputs, allow_rff=False)
    else:
        raise ValueError('Unsupported Cruise learner')


def _unpack(artifact):
    if (not isinstance(artifact, dict) or artifact.get('format') != FORMAT
            or artifact.get('taxonomy_version') != TAXONOMY_VERSION
            or artifact.get('feature_names') != list(audio_embedding.FEATURES)
            or artifact.get('feature_version') != audio_embedding.VERSION
            or not _hash(artifact.get('embedding_signature'))
            or not _hash(artifact.get('source_plan_sha256'))
            or artifact.get('aggregation') != 'mean_native_then_map'
            or artifact.get('combination') != 'equal_pmemo_deam'
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or artifact.get('encoding') != 'zlib-json-base64'
            or not _hash(artifact.get('payload_sha256'))
            or not isinstance(artifact.get('payload'), str)):
        raise ValueError('Incompatible Cruise artifact')
    try:
        compressed = base64.b64decode(artifact['payload'], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_PAYLOAD + 1)
        if (len(raw) > MAX_PAYLOAD or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail):
            raise ValueError('Cruise payload exceeds bounds or has trailing data')
        if hashlib.sha256(raw).hexdigest() != artifact['payload_sha256']:
            raise ValueError('Cruise payload checksum differs')
        payload = json.loads(raw)
    except (binascii.Error, zlib.error, UnicodeError, RecursionError) as error:
        raise ValueError('Invalid Cruise payload') from error
    if not isinstance(payload, dict) or set(payload) != set(SOURCES):
        raise ValueError('Cruise requires both frozen sources')
    for source in SOURCES:
        expert = payload[source]
        if (not isinstance(expert, dict) or expert.get('source') != source
                or expert.get('target_names') not in (['arousal', 'valence'], ['valence', 'arousal'])
                or not _hash(expert.get('source_expert_sha256'))):
            raise ValueError('Invalid Cruise source identity')
        _stage(expert.get('model'), audio_embedding.DIMENSIONS, 2)
    return {'header': artifact, 'experts': payload}


@lru_cache(maxsize=2)
def _load(path, modified, size):
    if size > MAX_BYTES:
        raise ValueError('Cruise artifact exceeds 256 KiB')
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Cruise artifact exceeds 256 KiB')
    try:
        model = _unpack(json.loads(raw))
    except RecursionError as error:
        raise ValueError('Invalid Cruise artifact nesting') from error
    model['sha256'] = hashlib.sha256(raw).hexdigest()
    return model


def load(path):
    path = Path(path)
    stat = path.stat()
    return _load(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def _predict(model, values):
    normalized = [(v - center) / scale for v, center, scale in
                  zip(values, model['mean'], model['scale'], strict=True)]
    if not all(_finite(v) for v in normalized):
        raise ValueError('Nonfinite Cruise normalized input')
    if model['kind'] == 'rff':
        width = model['features']
        projected = [math.cos(offset + sum(row[i] * value for row, value in
                     zip(model['frequencies'], normalized, strict=True))) * math.sqrt(2 / width)
                     for i, offset in enumerate(model['offsets'])]
        return _predict({'kind': 'ridge', **model['linear']}, projected)
    result = [intercept + sum(row[i] * value for row, value in
                             zip(model['weights'], normalized, strict=True))
              for i, intercept in enumerate(model['intercept'])]
    if not all(_finite(v) for v in result):
        raise ValueError('Nonfinite Cruise native output')
    return [max(0., min(1., value)) for value in result]


def chunks(values):
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_EXCERPTS
            or not all(_vector(row, audio_embedding.DIMENSIONS) and all(abs(v) <= 1e6 for v in row)
                       for row in values)):
        raise ValueError('Invalid Cruise excerpt embeddings')
    return values


def score(model, values):
    values = chunks(values)
    native, scores = {}, []
    for source in SOURCES:
        expert = model['experts'][source]
        predicted = [_predict(expert['model'], row) for row in values]
        averaged = [mean(column) for column in zip(*predicted, strict=True)]
        native[source] = dict(zip(expert['target_names'], averaged, strict=True))
        scores.append((1 - 2 * abs(native[source]['arousal'] - .5) + native[source]['valence']) / 2)
    return {'score': mean(scores), 'source_scores': dict(zip(SOURCES, scores, strict=True)),
            'native': native, 'sample_count': len(values),
            'aggregation': 'mean_native_then_map', 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    """Add selected-vibe suitability after successful shared-model inference.

    The shared ranking, threshold, top label and intensity inputs stay on their
    original scale. Only recommendation preparation consumes suitability.
    Optional-model failures preserve the complete shared prediction.
    """
    selected_mode, path = mode(), configured_path()
    if selected_mode == 'off' or not path:
        return prediction
    try:
        model = load(path)
        signatures = {audio_embedding.signature(sample) for sample in samples}
        if signatures != {model['header']['embedding_signature']}:
            return prediction
        candidate = score(model, [sample['embedding']['values'] for sample in samples])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return prediction
    suitability = {row['vibe']: row['score'] for row in prediction['ranking']}
    suitability.update(prediction.get('suitability', {}))
    if selected_mode == 'active':
        suitability['cruise'] = candidate['score']
    return {**prediction, 'suitability': suitability,
            'individual_models': {**prediction.get('individual_models', {}),
                                  'cruise': {**candidate, 'mode': selected_mode}}}


def route(group, values, signature, *, specialist_path=None, mode='shadow'):
    """Route only selected-vibe ranking; never compare mixed scales by argmax.

    The caller's best validated shared artifact remains authoritative for all
    other vibes and overall classification. Missing optional coefficients or an
    incompatible specialist leave all shared outputs usable.
    """
    if mode not in ('off', 'shadow', 'active'):
        raise ValueError('Unknown Cruise routing mode')
    shared.validate(group)
    if signature != group['embedding_signature']:
        raise ValueError('Incompatible shared embedding signature')
    values = chunks(values)
    embedding = [mean(column) for column in zip(*values, strict=True)]
    emotions = shared._predict(group['native'], embedding)
    calibrated = [max(v, 1e-8) for v in shared._predict(group['calibration'], emotions)]
    total = sum(calibrated)
    baseline = dict(zip(shared.LABELS, (v / total for v in calibrated), strict=True))
    result = {'shared_scores': baseline, 'suitability': dict(baseline),
              'shared_top_vibe': max(shared.LABELS, key=baseline.get), 'mode': mode,
              'individual_models': {}, 'applied_vibes': []}
    if mode == 'off' or specialist_path is None:
        return result
    try:
        model = load(specialist_path)
        if model['header']['embedding_signature'] != signature:
            return result
        candidate = score(model, values)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return result
    result['individual_models']['cruise'] = candidate
    if mode == 'active':
        result['suitability']['cruise'] = candidate['score']
        result['applied_vibes'] = ['cruise']
    return result


def route_samples(group, samples, *, specialist_path=None, mode='shadow'):
    """Production-shaped boundary: validate every excerpt's extractor identity."""
    if not samples or len(samples) > MAX_EXCERPTS:
        raise ValueError('Missing or excessive audio excerpts')
    signatures = {audio_embedding.signature(sample) for sample in samples}
    if len(signatures) != 1:
        raise ValueError('Mixed audio embedding provenance')
    return route(group, [sample['embedding']['values'] for sample in samples], signatures.pop(),
                 specialist_path=specialist_path, mode=mode)
