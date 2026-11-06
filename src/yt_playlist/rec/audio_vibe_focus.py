"""Frozen Reading/Studying heads for independent Focus suitability."""
from functools import lru_cache
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding, fao
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_runtime as bounded

FORMAT = 'audio-vibe-focus-1'
VERSION = 'focus-routing-1'
MAX_BYTES = 64 * 1024
DEFAULT_PATH = Path(__file__).with_name('data') / 'focus-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_FOCUS_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_FOCUS_MODE'
VIBE = 'concentrate'
TARGETS = ['Usage-Reading', 'Usage-Studying']
AGGREGATION = 'mean_clipped_activity_predictions'
TRAINING_AGGREGATION = 'mean_embedding_per_annotated_recording'


def configured_path():
    return bounded.configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    return 'active' if bounded.configured(MODE_KEY, 'active') == 'active' else 'off'


def identity():
    return bounded.identity(VERSION, mode(), configured_path(), load)


def validate(artifact):
    if (not isinstance(artifact, dict) or artifact.get('format') != FORMAT
            or artifact.get('taxonomy_version') != TAXONOMY_VERSION
            or artifact.get('feature_version') != audio_embedding.VERSION
            or artifact.get('feature_names') != list(audio_embedding.FEATURES)
            or artifact.get('vibe') != VIBE or artifact.get('procedure') != 'direct'
            or artifact.get('target_names') != TARGETS or artifact.get('weights') != [.5, .5]
            or artifact.get('aggregation') != AGGREGATION
            or artifact.get('training_aggregation') != TRAINING_AGGREGATION
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or any(not bounded.valid_hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or type(artifact.get('training_rows')) is not int or artifact['training_rows'] <= 0
            or not isinstance(artifact.get('heads'), list) or len(artifact['heads']) != len(TARGETS)):
        raise ValueError('Incompatible Focus model')
    for head in artifact['heads']:
        bounded.validate_stage(head, audio_embedding.DIMENSIONS, 1, allow_rff=False)
    return artifact


@lru_cache(maxsize=2)
def _load(path, mtime, size):
    return bounded.read_artifact(path, size, MAX_BYTES, lambda artifact: {'header': validate(artifact)})


def load(path):
    path = Path(path).resolve()
    info = fao.read(path).info
    return _load(str(path), info.identity, info.logical_bytes)


def score(model, values):
    values = bounded.chunks(values)
    native = {target: mean(bounded.predict(head, row)[0] for row in values)
              for target, head in zip(TARGETS, model['header']['heads'], strict=True)}
    return {'score': mean(native.values()), 'native': native, 'sample_count': len(values),
            'aggregation': AGGREGATION, 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    path = configured_path()
    return bounded.apply(samples, prediction, selected_mode=mode(), path=path,
                         loader=load, scorer=score, vibe=VIBE)
