"""Prepare optional, local-only model assets for the normal unpacked bridge.

Run fetch_model.py and build_runtime.py first. This copies verified artifacts;
it never downloads anything or changes the extension manifest. Store packaging
explicitly excludes this ignored research directory.
"""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / 'experiments/audio_lab'
DESTINATION = ROOT / 'extension/audio_model'
SCRIPTS = ('model_worker.js', 'runtime_loader.js', 'features.js', 'embedding_graph.js', 'audio_attributes.js')


def prepare(lab=LAB, destination=DESTINATION):
    vendor = lab / 'vendor'
    bundle = json.loads((vendor / 'bundle.json').read_text())
    runtime = json.loads((vendor / 'runtime.json').read_text())
    if {head['id'] for head in bundle.get('mood_heads', [])} != {'relaxed', 'aggressive', 'happy', 'sad', 'deam'} \
            or runtime['runtime'] != 'essentia-csp-audio-2' \
            or bundle.get('meyda_version') != '5.6.3' \
            or not {'meyda.min.js', 'meyda-LICENSE'}.issubset(row['path'] for row in bundle['files']) \
            or '-sDYNAMIC_EXECUTION=0' not in runtime['flags']:
        raise ValueError('Fetch the mood heads and Meyda, then build the CSP-compatible audio-2 runtime first')
    rows = [row for row in bundle['files'] if row['path'] != 'essentia-wasm.js'
            and not row['path'].startswith('voice/')]
    # Verify every artifact before replacing a previously usable installation.
    for row in rows + runtime['files']:
        data = (vendor / row['path']).read_bytes()
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('Research artifact changed: ' + row['path'])
    for name in SCRIPTS:
        if not (lab / name).is_file():
            raise ValueError('Missing worker source: ' + name)
    for row in rows + runtime['files']:
        target = destination / 'vendor' / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(vendor / row['path'], target)
    for name in SCRIPTS:
        shutil.copyfile(lab / name, destination / name)
    shutil.copyfile(vendor / 'runtime.json', destination / 'vendor/runtime.json')
    installed = {**bundle, 'files': rows, 'total_bytes': sum(row['bytes'] for row in rows),
                 'worker_sources': [{'path': name, 'bytes': (lab / name).stat().st_size,
                                     'sha256': hashlib.sha256((lab / name).read_bytes()).hexdigest()}
                                    for name in SCRIPTS]}
    (destination / 'vendor/bundle.json').write_text(json.dumps(installed, indent=2) + '\n')
    print(f'Prepared local-only model in {destination}. Reload the TuneConsole extension.')


if __name__ == '__main__':
    prepare()
