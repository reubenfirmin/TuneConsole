"""Keep the extractor bytes bound to existing research feature caches.

Production source may move independently. This reads original Git objects,
verifies previously recorded hashes, and writes only ignored research assets.
It never restores or changes the production files.
"""
import hashlib
import json
from pathlib import Path
import subprocess

from .external_experiment import LAB, OUT, ROOT, digest, save

NAMES = ('model_worker.js', 'audio_attributes.js', 'features.js',
         'embedding_graph.js', 'runtime_loader.js')
SNAPSHOT = LAB / 'local/runtime-checkpoint-530'


def original(name):
    return subprocess.check_output(['git', 'show', 'HEAD:' + name], cwd=ROOT)


def ensure():
    expected = json.loads((OUT / 'extractor-provenance.json').read_text())
    target = SNAPSHOT / 'audio_lab'
    files = {}
    for name in (*NAMES, 'mood_models.json', 'package.json'):
        path = target / name
        if not path.exists():
            data = original('experiments/audio_lab/' + name)
            if name in expected and hashlib.sha256(data).hexdigest() != expected[name]:
                raise ValueError('Git runtime differs from the recorded extractor: ' + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        actual = digest(path)
        if name in expected and actual != expected[name]:
            raise ValueError('Frozen research runtime changed: ' + name)
        files[name] = actual
    for name, source in (('vendor', LAB / 'vendor'), ('local/node', LAB / 'local/node')):
        link = target / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(source.resolve(), target_is_directory=True)
        if link.resolve() != source.resolve():
            raise ValueError('Unexpected runtime asset link')
    for name in ('vendor/bundle.json', 'vendor/runtime.json'):
        if digest(target / name) != expected[name]:
            raise ValueError('Frozen runtime asset manifest changed: ' + name)
    worker = SNAPSHOT / 'audio_lab_tools/external_audio_worker.mjs'
    if not worker.exists():
        data = original('experiments/audio_lab_tools/external_audio_worker.mjs')
        if hashlib.sha256(data).hexdigest() != expected['harness_sha256']:
            raise ValueError('Original harness differs from the recorded extractor')
        worker.parent.mkdir(parents=True, exist_ok=True)
        worker.write_bytes(data)
    if digest(worker) != expected['harness_sha256']:
        raise ValueError('Frozen research harness changed')
    files['../audio_lab_tools/external_audio_worker.mjs'] = digest(worker)
    manifest = SNAPSHOT / 'manifest.json'
    record = {'files': files, 'policy': 'Original runtime and harness bytes, verified against existing extractor provenance. Vendor and native-package links retain their separately recorded checksums.'}
    if manifest.exists() and json.loads(manifest.read_text()) != record:
        raise ValueError('Runtime snapshot manifest changed')
    save(manifest, record)
    return target, worker
