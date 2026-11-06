"""Frozen source-native fixed consensus for independent Uplift suitability."""
from pathlib import Path
from statistics import mean

from yt_playlist.rec import audio_vibe_runtime as bounded

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
    return bounded.configured(ENVIRONMENT_KEY, str(DEFAULT_PATH)) or None


def mode():
    return 'active' if bounded.configured(MODE_KEY, 'active') == 'active' else 'off'


def identity():
    return bounded.identity(VERSION, mode(), configured_path(), load)


def validate(artifact):
    return bounded.validate_consensus(artifact, {
        'format': FORMAT, 'vibe': 'uplift', 'aggregation': AGGREGATION, 'weights': WEIGHTS,
        'training_aggregation': TRAINING_AGGREGATION}, TARGETS)


load = bounded.consensus_loader(validate, MAX_BYTES)


def score(model, values):
    values = bounded.chunks(values)
    native, scores = {}, {}
    for source, targets in TARGETS.items():
        expert = model['header']['experts'][source]
        attributes = bounded.native_attributes(expert['model'], targets, values)
        native[source] = attributes
        # Soundtracks averages valence/happiness; all other sources have one target.
        scores[source] = mean(attributes.values())
    return {'score': sum(scores[source] * weight for source, weight in WEIGHTS.items()),
            'native': native, 'source_scores': scores, 'sample_count': len(values),
            'aggregation': AGGREGATION, 'sha256': model['sha256'],
            'score_semantics': 'independent_uncalibrated_suitability'}


def apply(samples, prediction):
    path = configured_path()
    return bounded.apply(samples, prediction, selected_mode=mode(), path=path,
                         loader=load, scorer=score, vibe='uplift')
