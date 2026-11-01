"""Frozen source-native consensus for independent Relax suitability.

Predict and clip each excerpt before averaging native attributes. The fixed
linear mappings then combine Emotify, Soundtracks, ManyMusic and Memo evidence.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import mean

from yt_playlist.core import audio_embedding
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_cruise as bounded

FORMAT = 'audio-vibe-relax-1'
VERSION = 'relax-routing-1'
MAX_BYTES = 256 * 1024
DEFAULT_PATH = Path(__file__).with_name('data') / 'relax-vibes.json'
ENVIRONMENT_KEY = 'YT_PLAYLIST_AUDIO_VIBE_RELAX_MODEL'
MODE_KEY = 'YT_PLAYLIST_AUDIO_VIBE_RELAX_MODE'
TARGETS = {'emotify-artist': ['calmness', 'tension'],
           'soundtracks-all': ['energy', 'tension', 'tender'],
           'manymusic-natural': ['valence', 'arousal'],
           'manymusic-generated': ['valence', 'arousal'],
           'memo2496-pilot': ['valence', 'arousal']}
WEIGHTS = {'emotify-artist': .5, 'soundtracks-all': .25,
           'manymusic-natural': .0625, 'manymusic-generated': .0625, 'memo2496-pilot': .125}
AGGREGATION = 'mean_clipped_native_then_fixed_map'


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
            or artifact.get('vibe') != 'relax' or artifact.get('aggregation') != AGGREGATION
            or artifact.get('score_semantics') != 'independent_uncalibrated_suitability'
            or artifact.get('weights') != WEIGHTS
            or any(not bounded._hash(artifact.get(key)) for key in
                   ('embedding_signature', 'source_model_sha256', 'source_plan_sha256'))
            or not isinstance(artifact.get('experts'), dict) or set(artifact['experts']) != set(TARGETS)):
        raise ValueError('Incompatible Relax model')
    for source, targets in TARGETS.items():
        expert = artifact['experts'][source]
        if (not isinstance(expert, dict) or expert.get('target_names') != targets
                or type(expert.get('training_rows')) is not int or expert['training_rows'] <= 0):
            raise ValueError('Invalid Relax source identity')
        bounded._stage(expert.get('model'), audio_embedding.DIMENSIONS, len(targets), allow_rff=False)
    return artifact


@lru_cache(maxsize=2)
def _load(path, mtime, size):
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Relax artifact exceeds size bound')
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != size:
        raise ValueError('Relax artifact changed during read')
    try:
        header = validate(json.loads(raw))
    except RecursionError as error:
        raise ValueError('Invalid Relax artifact nesting') from error
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
        if source == 'emotify-artist':
            scores[source] = (attributes['calmness'] + 1 - attributes['tension']) / 2
        elif source == 'soundtracks-all':
            scores[source] = (2 - attributes['energy'] - attributes['tension'] + attributes['tender']) / 3
        else:
            scores[source] = (1 - attributes['arousal'] + attributes['valence']) / 2
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
    suitability['relax'] = candidate['score']
    return {**prediction, 'suitability': suitability,
            'individual_models': {**prediction.get('individual_models', {}),
                                  'relax': {**candidate, 'mode': 'active'}}}
