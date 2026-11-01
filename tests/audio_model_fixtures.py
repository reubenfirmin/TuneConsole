"""Integrity-valid packaging fixtures; never used for inference."""
import hashlib
import json
from pathlib import Path
import shutil

from scripts.audio_model.bundle import HEADS, MODEL, SCRIPTS, VENDOR_FILES


def write_model_fixture(destination):
    destination = Path(destination)
    vendor = destination / 'vendor'
    vendor.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copyfile(MODEL / name, destination / name)
    rows = []
    for name in sorted(VENDOR_FILES | {'essentia-csp.js'}):
        data = ('fixture: ' + name).encode()
        path = vendor / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    (vendor / 'runtime.json').write_text(json.dumps({'schema': 1, 'runtime': 'essentia-csp-audio-2',
        'flags': ['-sDYNAMIC_EXECUTION=0'], 'files': [row for row in rows if row['path'] == 'essentia-csp.js']}))
    (vendor / 'bundle.json').write_text(json.dumps({'schema': 1, 'meyda_version': '5.6.3',
        'mood_heads': [{'id': head} for head in HEADS],
        'files': [row for row in rows if row['path'] != 'essentia-csp.js']}))
