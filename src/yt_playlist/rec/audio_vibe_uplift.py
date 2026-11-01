"""Frozen source-native fixed consensus for independent Uplift suitability."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_cruise as bounded

FORMAT = 'audio-vibe-uplift-1'
VERSION = 'uplift-routing-1'
MAX_BYTES = 256 * 1024
DEFAULT_PATH = Path(__file__).with_name('data') / 'uplift-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODE'
TARGETS = {'deam': ['valence'], 'pmemo': ['valence'],
           'soundtracks-all': ['valence', 'happy'], 'jamendo-human-4shards': ['happy'],
           'emotify-artist': ['joyful_activation'], 'manymusic-natural': ['valence'],
           'manymusic-generated': ['valence'], 'memo2496-pilot': ['valence']}
WEIGHTS = {'deam': .125, 'pmemo': .125, 'soundtracks-all': .25,
           'jamendo-human-4shards': .125, 'emotify-artist': .125,
           'manymusic-natural': .0625, 'manymusic-generated': .0625, 'memo2496-pilot': .125}
AGGREGATION = 'mean_clipped_native_then_fixed_map'
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
            or artifact.get('vibe') != 'uplift' or artifact.get('aggregation') != AGGREGATION
            or artifact.get('training_aggregation') != TRAINING_AGGREGATION
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or artifact.get('weights') != WEIGHTS
            or any(not bounded._hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or not isinstance(artifact.get('experts'), dict) or set(artifact['experts']) != set(TARGETS)):
        raise ValueError('Incompatible Uplift model')
    for source, targets in TARGETS.items():
        expert = artifact['experts'][source]
        if (not isinstance(expert, dict) or expert.get('target_names') != targets
                or type(expert.get('training_rows')) is not int or expert['training_rows'] <= 0):
            raise ValueError('Invalid Uplift source identity')
        bounded._stage(expert.get('model'), audio_embedding.DIMENSIONS, len(targets), allow_rff=False)
    return artifact


@lru_cache(maxsize=2)
def _load(path, mtime, size):
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Uplift artifact exceeds size bound')
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != size:
        raise ValueError('Uplift artifact changed during read')
    try:
        header = validate(json.loads(raw))
    except RecursionError as error:
        raise ValueError('Invalid Uplift artifact nesting') from error
    return {'header': header, 'sha256': hashlib.sha256(raw).hexdigest()}


def load(path):
    path = Path(path).resolve()
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, {}
    for source, targets in TARGETS.items():
        expert = model['header']['experts'][source]
        predicted = [bounded._predict(expert['model'], row) for row in values]
        attributes = dict(zip(targets, (mean(column) for column in zip(*predicted, strict=True)), strict=True))
        native[source] = attributes
        # Soundtracks averages valence/happiness; all other sources have one target.
        scores[source] = mean(attributes.values())
    return {'score': sum(scores[source] * weight for source, weight in WEIGHTS.items()),
            'native': native, 'source_scores': scores, 'sample_count': len(values),
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
    suitability['uplift'] = candidate['score']
    return {**prediction, 'suitability': suitability,
            'individual_models': {**prediction.get('individual_models', {}),
                                  'uplift': {**candidate, 'mode': 'active'}}}
