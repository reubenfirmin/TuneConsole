"""File capability: whether the bundled browser-extension audio model is installed."""
from pathlib import Path

MODEL = Path(__file__).resolve().parents[3] / 'extension' / 'audio_model'
ASSETS = frozenset({
    'model_worker.js', 'runtime_loader.js', 'features.js', 'embedding_graph.js', 'audio_attributes.js',
    'vendor/bundle.json', 'vendor/runtime.json', 'vendor/essentia-csp.js',
    'vendor/tf.min.js', 'vendor/meyda.min.js', 'vendor/musicnn/model.json',
    'vendor/musicnn/group1-shard1of1.bin', 'vendor/musicnn/metadata.json',
} | {f'vendor/{head}/{name}' for head in ('relaxed', 'aggressive', 'happy', 'sad', 'deam')
     for name in ('model.json', 'group1-shard1of1.bin', 'metadata.json')})


def available(model_dir=MODEL, assets=ASSETS) -> bool:
    return all((model_dir / name).is_file() for name in assets)
