"""Frozen source-native consensus for independent Relax suitability.

Predict and clip each excerpt before averaging native attributes. The fixed
linear mappings then combine Emotify, Soundtracks, ManyMusic and Memo evidence.
"""
from pathlib import Path

from yt_playlist.rec import audio_vibe_runtime as bounded

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
    return bounded.configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    return 'active' if bounded.configured(MODE_KEY, 'active') == 'active' else 'off'


def identity():
    return bounded.identity(VERSION, mode(), configured_path(), load)


def validate(artifact):
    return bounded.validate_consensus(artifact, {
        'format': FORMAT, 'vibe': 'relax', 'aggregation': AGGREGATION, 'weights': WEIGHTS}, TARGETS)


load = bounded.consensus_loader(validate, MAX_BYTES)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, {}
    for source, targets in TARGETS.items():
        expert = model['header']['experts'][source]
        attributes = bounded.native_attributes(expert['model'], targets, values)
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
    return bounded.apply(samples, prediction, selected_mode=mode(), path=path,
                         loader=load, scorer=score, vibe='relax')
