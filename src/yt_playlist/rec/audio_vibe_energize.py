"""Frozen activation, energy and power consensus for Energize suitability."""
from functools import lru_cache
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding, fao
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_runtime as bounded

FORMAT = 'audio-vibe-energize-1'
VERSION = 'energize-routing-1'
MAX_BYTES = 256 * 1024
DEFAULT_PATH = Path(__file__).with_name('data') / 'energize-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_ENERGIZE_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_ENERGIZE_MODE'
TARGETS = {'deam': ['arousal'], 'pmemo': ['arousal'], 'soundtracks-all': ['energy'],
           'emotify-artist': ['power'], 'manymusic-natural': ['arousal'],
           'manymusic-generated': ['arousal'], 'memo2496-pilot': ['arousal']}
WEIGHTS = {'deam': .125, 'pmemo': .125, 'soundtracks-all': .25, 'emotify-artist': .25,
           'manymusic-natural': .0625, 'manymusic-generated': .0625, 'memo2496-pilot': .125}
AGGREGATION = 'mean_clipped_native_then_fixed_map'
TRAINING_AGGREGATION = 'mean_embedding_per_annotated_recording'
PROCEDURE = 'consensus_no_joy'


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
            or artifact.get('vibe') != 'energize' or artifact.get('aggregation') != AGGREGATION
            or artifact.get('training_aggregation') != TRAINING_AGGREGATION
            or artifact.get('procedure') != PROCEDURE
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or artifact.get('weights') != WEIGHTS
            or any(not bounded.valid_hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or not isinstance(artifact.get('experts'), dict) or set(artifact['experts']) != set(TARGETS)):
        raise ValueError('Incompatible Energize model')
    for source, targets in TARGETS.items():
        expert = artifact['experts'][source]
        if (not isinstance(expert, dict) or expert.get('target_names') != targets
                or type(expert.get('training_rows')) is not int or expert['training_rows'] <= 0):
            raise ValueError('Invalid Energize source identity')
        bounded.validate_stage(expert.get('model'), audio_embedding.DIMENSIONS, len(targets), allow_rff=False)
    return artifact


@lru_cache(maxsize=2)
def _load(path, mtime, size):
    return bounded.read_artifact(path, size, MAX_BYTES, lambda artifact: {'header': validate(artifact)})


def load(path):
    path = Path(path).resolve()
    info = fao.stat(path)
    return _load(str(path), info.identity, info.logical_bytes)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, {}
    for source, targets in TARGETS.items():
        expert = model['header']['experts'][source]
        scores[source] = mean(bounded.predict(expert['model'], row)[0] for row in values)
        native[source] = {targets[0]: scores[source]}
    return {'score': sum(scores[source] * weight for source, weight in WEIGHTS.items()),
            'native': native, 'source_scores': scores, 'sample_count': len(values),
            'aggregation': AGGREGATION, 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    path = configured_path()
    return bounded.apply(samples, prediction, selected_mode=mode(), path=path,
                         loader=load, scorer=score, vibe='energize')
