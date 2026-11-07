"""Frozen Cruise suitability, composed with five other vibe specialists.

It reads bundled coefficients, never annotations or experiment files.
Individual suitability is used only within a selected vibe; overall six-way
classification continues to use the shared model's comparable scores.
"""
import base64
import binascii
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import mean
import zlib

from yt_playlist.core import audio_embedding, fao
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_runtime as bounded

FORMAT = 'audio-vibe-cruise-1'
MAX_BYTES = 256 * 1024
MAX_PAYLOAD = 1024 * 1024
MAX_EXCERPTS = bounded.MAX_EXCERPTS
SOURCES = ('pmemo', 'deam')
VERSION = 'cruise-routing-1'
DEFAULT_PATH = Path(__file__).with_name('data') / 'cruise-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_CRUISE_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_CRUISE_MODE'


def configured_path():
    return bounded.configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    value = bounded.configured(MODE_KEY, 'active')
    return value if value in ('active', 'shadow', 'off') else 'off'


def identity():
    """Include routing policy and mode in bounded background invalidation."""
    return bounded.identity(VERSION, mode(), configured_path(), load)


def _unpack(artifact):
    if (not isinstance(artifact, dict) or artifact.get('format') != FORMAT
            or artifact.get('taxonomy_version') != TAXONOMY_VERSION
            or artifact.get('feature_names') != list(audio_embedding.FEATURES)
            or artifact.get('feature_version') != audio_embedding.VERSION
            or not bounded.valid_hash(artifact.get('embedding_signature'))
            or not bounded.valid_hash(artifact.get('source_plan_sha256'))
            or artifact.get('aggregation') != 'mean_native_then_map'
            or artifact.get('combination') != 'equal_pmemo_deam'
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or artifact.get('encoding') != 'zlib-json-base64'
            or not bounded.valid_hash(artifact.get('payload_sha256'))
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
                or not bounded.valid_hash(expert.get('source_expert_sha256'))):
            raise ValueError('Invalid Cruise source identity')
        bounded.validate_stage(expert.get('model'), audio_embedding.DIMENSIONS, 2)
    return {'header': artifact, 'experts': payload}


@lru_cache(maxsize=2)
def _load(path, modified, size):
    return bounded.read_artifact(path, size, MAX_BYTES, _unpack, exact_size=False)


def load(path):
    path = Path(path)
    info = fao.read(path).info
    return _load(str(path.resolve()), info.identity, info.logical_bytes)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, []
    for source in SOURCES:
        expert = model['experts'][source]
        predicted = [bounded.predict(expert['model'], row) for row in values]
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
    return bounded.apply(samples, prediction, selected_mode=mode(), path=configured_path(),
                         loader=load, scorer=score, vibe='cruise')
