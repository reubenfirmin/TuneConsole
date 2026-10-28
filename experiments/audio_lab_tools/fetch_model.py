"""Fetch an isolated research bundle; never runs as part of installation or tests.

python experiments/audio_lab_tools/fetch_model.py
No audio is acquired. The ignored vendor directory is not a distribution bundle.
"""
import hashlib
import json
from pathlib import Path
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1] / 'audio_lab' / 'vendor'
REVISION = '819c00e8f034dc709e3d330ed8ac9ed37dfdb767'
GITHUB = f'https://raw.githubusercontent.com/MTG/essentia.js/{REVISION}/'
MODELS = 'https://essentia.upf.edu/models/'
ASSETS = {
    'meyda.min.js': 'https://cdn.jsdelivr.net/npm/meyda@5.6.3/dist/web/meyda.min.js',
    'meyda-LICENSE': 'https://cdn.jsdelivr.net/npm/meyda@5.6.3/LICENSE.md',
    'tf.min.js': 'https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@3.18.0/dist/tf.min.js',
    'essentia-wasm.js': GITHUB + 'examples/starter-code/autotagging/lib/essentia-wasm.module.js',
    'essentia-LICENSE': GITHUB + 'LICENSE',
    'model-LICENSE': MODELS + 'LICENSE',
    'musicnn/model.json': GITHUB + 'examples/starter-code/autotagging/models/msd-musicnn-1/model.json',
    'musicnn/group1-shard1of1.bin': GITHUB + 'examples/starter-code/autotagging/models/msd-musicnn-1/group1-shard1of1.bin',
    'musicnn/metadata.json': MODELS + 'autotagging/msd/msd-musicnn-1.json',
    'voice/model.json': MODELS + 'classification-heads/voice_instrumental/voice_instrumental-msd-musicnn-1-tfjs/model.json',
    'voice/group1-shard1of1.bin': MODELS + 'classification-heads/voice_instrumental/voice_instrumental-msd-musicnn-1-tfjs/group1-shard1of1.bin',
    'voice/metadata.json': MODELS + 'classification-heads/voice_instrumental/voice_instrumental-msd-musicnn-1.json',
}
MOODS = json.loads((ROOT.parent / 'mood_models.json').read_text())
for head in MOODS:
    for name in ('model.json', 'group1-shard1of1.bin'):
        ASSETS[f"{head['id']}/{name}"] = MODELS + f"classification-heads/{head['path']}-tfjs/{name}"
    ASSETS[f"{head['id']}/metadata.json"] = MODELS + f"classification-heads/{head.get('metadata', head['path'])}.json"


def main():
    ROOT.mkdir(exist_ok=True)
    manifest = {'schema': 1, 'purpose': 'local_research_only', 'essentia_js_commit': REVISION,
                'tensorflow_js_version': '3.18.0', 'meyda_version': '5.6.3', 'mood_heads': MOODS, 'files': []}
    previous = ROOT / 'bundle.json'
    known = {row['path']: row for row in json.loads(previous.read_text())['files']} if previous.exists() else {}
    total = 0
    for name, url in ASSETS.items():
        path = ROOT / name
        if path.exists() and name in known:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != known[name]['sha256']:
                raise ValueError(f'Local artifact changed: {name}; inspect it before fetching again')
        else:
            request = urllib.request.Request(url, headers={'User-Agent': 'TuneConsole-audio-feasibility/1'})
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        data = response.read(12 * 1024 * 1024 + 1)
                    break
                except (TimeoutError, urllib.error.URLError):
                    if attempt == 2:
                        raise
                    print(f'Retrying {name}', flush=True)
            if len(data) > 12 * 1024 * 1024:
                raise ValueError(f'Oversized artifact: {name}')
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        total += len(data)
        if total > 24 * 1024 * 1024:
            raise ValueError('Research bundle exceeds its download budget')
        manifest['files'].append({'path': name, 'url': url, 'bytes': len(data),
                                  'sha256': hashlib.sha256(data).hexdigest()})
        print(f'{name}: {len(data)} bytes', flush=True)
    manifest['total_bytes'] = total
    previous.write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Local research bundle: {total} bytes. Read vendor/model-LICENSE before distribution.')


if __name__ == '__main__':
    main()
