"""Bundled external-emotion model and optional local audio-vibe corrections.

Inference uses audio measurements and exported model coefficients. The raw human
labels used to develop the model are not runtime inputs or per-track overrides;
they remain confined to development collection, training and evaluation.
"""
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core import features
from yt_playlist.core import audio_vibe_metadata as metadata_features
from yt_playlist.core import audio_embedding
from yt_playlist.rec import audio_vibe_external

SCHEMA = 1
LEGACY_FEATURE_VERSION = 'audio-vibe-corrections-1'
FEATURE_VERSION = 'audio-vibe-corrections-2/' + metadata_features.VERSION
EMBEDDING_FEATURE_VERSION = 'audio-vibe-corrections-3/' + audio_embedding.VERSION + '/' + metadata_features.VERSION
LABELS = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')
AXES = ('relaxed', 'aggression', 'happiness', 'sadness', 'intensity', 'emotional_tone')
ATTRIBUTES = ('tempo_bpm', 'tempo_confidence', 'beat_interval_cv',
              'peak_normalized_rms_dbfs', 'excerpt_loudness_range_lu',
              'spectral_centroid_hz', 'spectral_flatness')
AUDIO_FEATURES = tuple('fit.' + key for key in LABELS) + tuple('median.' + key for key in AXES) + (
    'span.intensity', 'span.aggression', 'span.relaxed', 'change.intensity', 'change.aggression',
) + tuple('audio.' + key for key in ATTRIBUTES)
FEATURES = AUDIO_FEATURES + metadata_features.FEATURES
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_MODEL'
THRESHOLD = .65
DEFAULT_PATH = Path(__file__).with_name('data') / 'emotify-vibes.json'


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def signature(sample):
    """Do not silently reuse a fit after changing the upstream measurements/models."""
    attributes = sample.get('audio_attributes') or {}
    source = {key: sample.get(key) for key in ('model', 'model_version', 'mood_versions', 'artifact_hashes')}
    source['audio_attributes'] = {key: attributes.get(key) for key in ('version', 'runtime', 'meyda_version')}
    return hashlib.sha256(json.dumps(source, sort_keys=True, allow_nan=False).encode()).hexdigest()


def vector(samples, prediction, metadata=None, *, feature_version=FEATURE_VERSION, feature_names=FEATURES):
    """Ordered audio features and catalog metadata, without source-specific weights."""
    embedding = audio_embedding.vector(samples) if feature_version == EMBEDDING_FEATURE_VERSION else []
    if feature_version == EMBEDDING_FEATURE_VERSION and tuple(feature_names) == audio_embedding.FEATURES:
        return embedding
    excerpts = prediction['arc']['excerpts']
    values = [next(row['score'] for row in prediction['ranking'] if row['vibe'] == key) for key in LABELS]
    columns = {axis: [row['dimensions'][axis] for row in excerpts] for axis in AXES}
    values.extend(median(columns[axis]) for axis in AXES)
    values.extend(max(columns[axis]) - min(columns[axis]) for axis in ('intensity', 'aggression', 'relaxed'))
    values.extend(columns[axis][-1] - columns[axis][0] for axis in ('intensity', 'aggression'))
    for key in ATTRIBUTES:
        column = [(sample.get('audio_attributes') or {}).get('values', {}).get(key) for sample in samples]
        if not all(finite(value) for value in column):
            raise ValueError('Missing or nonfinite audio measurement: ' + key)
        values.append(median(column))
    if len(values) != len(AUDIO_FEATURES) or not all(finite(value) for value in values):
        raise ValueError('Invalid listening model inputs')
    if feature_version != LEGACY_FEATURE_VERSION:
        values.extend(metadata_features.vector(metadata, subgenres=metadata_features.vocabulary(feature_names)))
    if feature_version == EMBEDDING_FEATURE_VERSION:
        # The subgenre vocabulary follows the fixed audio/catalog/embedding columns.
        split = len(FEATURES)
        values = values[:split] + embedding + values[split:]
    return values


def margin(score):
    """Reference margin, not a claim that the heuristic score is a probability."""
    value = max(.001, min(.999, score))
    return math.log(value / (1 - value)) - math.log(THRESHOLD / (1 - THRESHOLD))


def corrected_score(score, adjustment, baseline_weight=1.):
    value = max(-40, min(40, baseline_weight * margin(score) + adjustment + math.log(THRESHOLD / (1 - THRESHOLD))))
    return 1 / (1 + math.exp(-value))


def scores(model, values, baseline):
    """The trainer and runtime use identical normalization and score corrections."""
    normalized = [0. if value is None else (value - center) / scale for value, center, scale in
                  zip(values, model['center'], model['scale'], strict=True)]
    if not all(finite(value) for value in normalized):
        raise ValueError('Nonfinite normalized listening features')
    if not model['classifiers']:
        return dict(baseline)
    result = dict(baseline)
    logits = {key: margin(value) for key, value in baseline.items()}
    for key, classifier in model['classifiers'].items():
        adjustment = classifier['intercept'] + sum(w * x for w, x in
                                                  zip(classifier['weights'], normalized, strict=True))
        if not finite(adjustment):
            raise ValueError('Nonfinite listening correction')
        result[key] = round(corrected_score(baseline[key], adjustment, classifier.get('baseline_weight', 1.)), 5)
        logits[key] = classifier.get('baseline_weight', 1.) * margin(baseline[key]) + adjustment
    if model.get('score_link') == 'softmax':
        largest = max(logits.values())
        positive = {key: math.exp(value - largest) for key, value in logits.items()}
        total = sum(positive.values())
        result = {key: value / total for key, value in positive.items()}
    return result


def validate(model):
    if model.get('format') == audio_vibe_external.FORMAT:
        return audio_vibe_external.validate(model)
    names = model.get('features')
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError('Invalid listening feature vocabulary')
    subgenres = metadata_features.vocabulary(names)
    if (len(subgenres) > 2048 or tuple(sorted(set(subgenres))) != subgenres
            or any(metadata_features.subgenre({'genre': name}) != name for name in subgenres)):
        raise ValueError('Invalid listening subgenre vocabulary')
    version = model.get('feature_version')
    if version == LEGACY_FEATURE_VERSION:
        expected = AUDIO_FEATURES
    elif version == EMBEDDING_FEATURE_VERSION:
        expected = (audio_embedding.FEATURES if tuple(names) == audio_embedding.FEATURES else
                    FEATURES + audio_embedding.FEATURES + tuple(metadata_features.SUBGENRE_PREFIX + name for name in subgenres))
        digest = model.get('embedding_signature')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid listening embedding provenance')
    else:
        expected = FEATURES + tuple(metadata_features.SUBGENRE_PREFIX + name for name in subgenres)
    if (model.get('schema') != SCHEMA or version not in (FEATURE_VERSION, LEGACY_FEATURE_VERSION, EMBEDDING_FEATURE_VERSION)
            or model.get('taxonomy_version') != TAXONOMY_VERSION or model.get('features') != list(expected)
            or model.get('threshold') != THRESHOLD or not isinstance(model.get('source_signature'), str)
            or len(model['source_signature']) != 64):
        raise ValueError('Incompatible listening model')
    for key in ('center', 'scale'):
        values = model.get(key)
        if (not isinstance(values, list) or len(values) != len(expected)
                or not all(finite(value) and (key != 'scale' or value > 0) for value in values)):
            raise ValueError('Invalid listening model normalization')
    classifiers = model.get('classifiers')
    if not isinstance(classifiers, dict) or not set(classifiers) <= set(LABELS):
        raise ValueError('Invalid listening model targets')
    if (model.get('score_link', 'sigmoid') not in ('sigmoid', 'softmax')
            or (model.get('score_link') == 'softmax' and classifiers and set(classifiers) != set(LABELS))):
        raise ValueError('Invalid listening score link')
    for classifier in classifiers.values():
        weights = classifier.get('weights')
        if (not isinstance(weights, list) or len(weights) != len(expected)
                or not all(finite(value) for value in weights) or not finite(classifier.get('intercept'))
                or not finite(classifier.get('baseline_weight', 1.))
                or not 0 <= classifier.get('baseline_weight', 1.) <= 1):
            raise ValueError('Invalid listening model coefficients')
    return model


@lru_cache(maxsize=4)
def _load(path, modified, size):
    if size > 262144:
        raise ValueError('Listening model exceeds 256 KiB')
    with Path(path).open('rb') as stream:
        content = stream.read(262145)
    if len(content) > 262144:
        raise ValueError('Listening model exceeds 256 KiB')
    model = validate(json.loads(content))
    return model, hashlib.sha256(content).hexdigest()


def configured_path():
    """Use the bundled model unless overridden; an explicit empty value disables it."""
    if ENVIRONMENT_KEY in os.environ:
        return os.environ[ENVIRONMENT_KEY] or None
    try:
        for line in features.LOCAL_ENV.read_text().splitlines():
            name, separator, value = line.strip().partition('=')
            if separator and name.strip() == ENVIRONMENT_KEY:
                return value.strip().strip('\"\'') or None
    except OSError:
        pass
    return str(DEFAULT_PATH)


def identity():
    """Cheap cached artifact identity for background prediction invalidation."""
    path = configured_path()
    if path:
        try:
            stat = Path(path).stat()
            return _load(path, stat.st_mtime_ns, stat.st_size)[1]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
    return 'heuristic'


def apply(samples, prediction, metadata=None):
    """An absent, invalid or incompatible local artifact leaves the heuristic intact."""
    path = configured_path()
    if not path:
        return prediction
    try:
        stat = Path(path).stat()
        model, digest = _load(path, stat.st_mtime_ns, stat.st_size)
        external = model.get('format') == audio_vibe_external.FORMAT
        if external:
            if prediction.get('taxonomy_version') != model['taxonomy_version']:
                return prediction
            corrected = audio_vibe_external.scores(model, samples)
            trained_labels = model['labels']
            score_source = 'external_emotion_model'
            evidence = 'Includes learned external-emotion evidence; scores remain uncalibrated'
        else:
            if not model['classifiers']:
                return prediction
            if model.get('heuristic_version') != prediction['version']:
                return prediction
            if any(signature(sample) != model['source_signature'] for sample in samples):
                return prediction
            if (model['feature_version'] == EMBEDDING_FEATURE_VERSION
                    and any(audio_embedding.signature(sample) != model['embedding_signature'] for sample in samples)):
                return prediction
            corrected = scores(model, vector(samples, prediction, metadata, feature_version=model['feature_version'],
                                             feature_names=model['features']),
                               {row['vibe']: row['score'] for row in prediction['ranking']})
            trained_labels = model['classifiers']
            score_source = 'local_learned_correction'
            evidence = 'Includes a local learned correction; scores remain uncalibrated'
        ranking = []
        for row in prediction['ranking']:
            if row['vibe'] in trained_labels:
                row = {**row, 'baseline_score': row['score'],
                       'score': corrected[row['vibe']],
                       'score_source': score_source}
            ranking.append(row)
        if external:
            # Research argmax breaks ties in the fixed target order, including
            # the uniform output when every calibrated value clips to zero.
            ranking.sort(key=lambda row: (-row['score'], trained_labels.index(row['vibe'])))
        else:
            ranking.sort(key=lambda row: row['score'], reverse=True)
        matches = [row for row in ranking if row['score'] >= THRESHOLD]
        method = 'strong_fit' if matches else 'best_available'
        matches = matches or ranking[:1]
        if model['training'].get('objective') in ('acceptable_nll', 'acceptable_libra'):
            matches, method = ranking[:1], 'top_ranked'
        return {**prediction, 'vibe': matches[0]['vibe'], 'vibes': [row['vibe'] for row in matches],
                'label': ' · '.join(row['label'] for row in matches), 'ranking': ranking,
                'selection_method': method, 'score_margin': round(ranking[0]['score'] - ranking[1]['score'], 5),
                'model': {'sha256': digest, 'feature_version': model['feature_version'],
                          'trained_labels': sorted(trained_labels),
                          'training_songs': model['training']['songs']},
                'evidence': prediction['evidence'] + [evidence]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return prediction
