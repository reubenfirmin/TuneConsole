"""Frozen Reading/Studying heads for independent Focus suitability."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_cruise as bounded

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
    return bounded._configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    return 'active' if bounded._configured(MODE_KEY, 'active') == 'active' else 'off'


def identity():
    selected_mode, path = mode(), configured_path()
    if selected_mode == 'active' and path:
        try:
            return '/'.join((VERSION, selected_mode, load(path)['sha256']))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
    return '/'.join((VERSION, selected_mode, 'shared'))


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
            or any(not bounded._hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or type(artifact.get('training_rows')) is not int or artifact['training_rows'] <= 0
            or not isinstance(artifact.get('heads'), list) or len(artifact['heads']) != len(TARGETS)):
        raise ValueError('Incompatible Focus model')
    for head in artifact['heads']:
        bounded._stage(head, audio_embedding.DIMENSIONS, 1, allow_rff=False)
    return artifact


@lru_cache(maxsize=2)
def _load(path, mtime, size):
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Focus artifact exceeds size bound')
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != size:
        raise ValueError('Focus artifact changed during read')
    try:
        header = validate(json.loads(raw))
    except RecursionError as error:
        raise ValueError('Invalid Focus artifact nesting') from error
    return {'header': header, 'sha256': hashlib.sha256(raw).hexdigest()}


def load(path):
    path = Path(path).resolve()
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def score(model, values):
    values = bounded.chunks(values)
    native = {target: mean(bounded._predict(head, row)[0] for row in values)
              for target, head in zip(TARGETS, model['header']['heads'], strict=True)}
    return {'score': mean(native.values()), 'native': native, 'sample_count': len(values),
            'aggregation': AGGREGATION, 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    path = configured_path()
    if mode() != 'active' or not path:
        return prediction
    try:
        model = load(path)
        if {audio_embedding.signature(sample) for sample in samples} != {model['header']['embedding_signature']}:
            return prediction
        candidate = score(model, [sample['embedding']['values'] for sample in samples])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return prediction
    suitability = {row['vibe']: row['score'] for row in prediction['ranking']}
    suitability.update(prediction.get('suitability', {}))
    suitability[VIBE] = candidate['score']
    return {**prediction, 'suitability': suitability,
            'individual_models': {**prediction.get('individual_models', {}),
                                  VIBE: {**candidate, 'mode': 'active'}}}
