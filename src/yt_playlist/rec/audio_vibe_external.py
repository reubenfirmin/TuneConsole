"""Frozen external-emotion transfer model; bounded JSON and inference only.

MusiCNN excerpt means feed nine Emotify emotion regressors, then a six-vibe
regressor. Both stages clip to [0, 1]; the last stage is normalized exactly as
in the grouped research evaluation. These are relative, uncalibrated scores.
"""
import math

from yt_playlist.core import audio_embedding
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION

FORMAT = 'audio-vibe-emotify-1'
FEATURE_VERSION = FORMAT + '/' + audio_embedding.VERSION
LABELS = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')
EMOTIONS = ('amazement', 'solemnity', 'tenderness', 'nostalgia', 'calmness',
            'power', 'joyful_activation', 'tension', 'sadness')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(values, length, *, positive=False):
    return (isinstance(values, list) and len(values) == length
            and all(_finite(n) and (not positive or n > 0) for n in values))


def _validate_stage(stage, inputs, outputs):
    if (not isinstance(stage, dict)
            or not _vector(stage.get('mean'), inputs)
            or not _vector(stage.get('scale'), inputs, positive=True)
            or not _vector(stage.get('intercept'), outputs)
            or not isinstance(stage.get('weights'), list) or len(stage['weights']) != inputs
            or not all(_vector(row, outputs) for row in stage['weights'])):
        raise ValueError('Invalid external-emotion coefficients')


def validate(model):
    digest = model.get('embedding_signature')
    training = model.get('training')
    if (model.get('format') != FORMAT or model.get('feature_version') != FEATURE_VERSION
            or model.get('taxonomy_version') != TAXONOMY_VERSION
            or model.get('labels') != list(LABELS) or model.get('emotions') != list(EMOTIONS)
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)
            or not isinstance(training, dict) or training.get('objective') != 'acceptable_nll'
            or any(type(training.get(key)) is not int or training[key] <= 0
                   for key in ('songs', 'native_songs'))):
        raise ValueError('Incompatible external-emotion model')
    _validate_stage(model.get('native'), audio_embedding.DIMENSIONS, len(EMOTIONS))
    _validate_stage(model.get('calibration'), len(EMOTIONS), len(LABELS))
    return model


def _predict(stage, values):
    normalized = [(value - mean) / scale for value, mean, scale in
                  zip(values, stage['mean'], stage['scale'], strict=True)]
    if not all(_finite(value) for value in normalized):
        raise ValueError('Nonfinite external-emotion input')
    result = [intercept + sum(row[i] * value for row, value in
                             zip(stage['weights'], normalized, strict=True))
              for i, intercept in enumerate(stage['intercept'])]
    if not all(_finite(value) for value in result):
        raise ValueError('Nonfinite external-emotion output')
    return [max(0., min(1., value)) for value in result]


def scores(model, samples):
    if not samples or any(audio_embedding.signature(sample) != model['embedding_signature']
                          for sample in samples):
        raise ValueError('Incompatible external-emotion embedding')
    emotions = _predict(model['native'], audio_embedding.vector(samples))
    values = [max(value, 1e-8) for value in _predict(model['calibration'], emotions)]
    total = sum(values)
    return dict(zip(LABELS, (value / total for value in values), strict=True))
