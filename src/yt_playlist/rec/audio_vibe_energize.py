"""Frozen activation, energy and power consensus for Energize suitability."""
from pathlib import Path
from statistics import mean

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
    return bounded.validate_consensus(artifact, {
        'format': FORMAT, 'vibe': 'energize', 'aggregation': AGGREGATION, 'weights': WEIGHTS,
        'training_aggregation': TRAINING_AGGREGATION, 'procedure': PROCEDURE}, TARGETS)


load = bounded.consensus_loader(validate, MAX_BYTES)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, {}
    for source, targets in TARGETS.items():
        expert = model['header']['experts'][source]
        native[source] = bounded.native_attributes(expert['model'], targets, values)
        scores[source] = mean(native[source].values())
    return {'score': sum(scores[source] * weight for source, weight in WEIGHTS.items()),
            'native': native, 'source_scores': scores, 'sample_count': len(values),
            'aggregation': AGGREGATION, 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    path = configured_path()
    return bounded.apply(samples, prediction, selected_mode=mode(), path=path,
                         loader=load, scorer=score, vibe='energize')
