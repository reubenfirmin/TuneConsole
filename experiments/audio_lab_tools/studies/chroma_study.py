"""Prospective chroma-only and embedding-plus-chroma representation study."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .. import source_path
from ..support import quality_round as evidence
from . import representation_study as study
from ..support.external_experiment import LAB, digest, pcm, save
from ..focus.focus_experiment import arrays, check_receipt, inside, load
from ..audio.quality_reference_audio import response

ROOT = LAB / 'local/chroma-810'
PYTHON = ROOT / 'venv/bin/python'
DEFAULT = LAB / 'local/chroma-810-20261101/round-1'
DOCUMENT = LAB / 'CHROMA_PLAN.md'
WORKER = source_path('chroma_worker.py')


def environment():
    ROOT.mkdir(parents=True, exist_ok=True)
    cache = str(ROOT / 'uv-cache')
    subprocess.run(['uv', '--cache-dir', cache, 'venv', '--python', '3.12', str(ROOT / 'venv')],
                   check=True, timeout=120)
    subprocess.run(['uv', '--cache-dir', cache, 'pip', 'install', '--python', str(PYTHON),
        'librosa==0.11.0', 'numpy==1.26.4', 'scipy==1.14.1', 'numba==0.61.2',
        'llvmlite==0.44.0', 'soundfile==0.13.1', 'soxr==0.5.0.post1'], check=True, timeout=780)
    lock = subprocess.check_output(['uv', '--cache-dir', cache, 'pip', 'freeze', '--python', str(PYTHON)], text=True)
    (ROOT / 'requirements.lock').write_text(lock)
    print(lock, flush=True)


def freeze(directory):
    evidence.verify(study.ORIGINAL)
    old = load(study.ORIGINAL / 'plan.json')
    inputs = {name.removeprefix('inputs/'): study.ORIGINAL / name for name in old['inputs']}
    inputs.update({'baseline-features.npz': study.ORIGINAL / 'features.npz',
        'baseline-predictions.npz': study.ORIGINAL / 'predictions.npz',
        'baseline-rows.json': study.ORIGINAL / 'extraction.json'})
    specification = {'document_sha256': digest(DOCUMENT), 'baseline_variant': 'musicnn',
        'new_variants': ['chroma', 'combined'], 'variants': ['musicnn', 'chroma', 'combined'],
        'cohort': old['protocol'], 'acceptance': old['protocol']['acceptance'],
        'dimensions': {'musicnn': 200, 'chroma': 72, 'combined': 272},
        'budgets': {'extract_seconds': 3600, 'fit_evaluate_seconds': 3600,
                    'rss_bytes': 2 * 1024**3, 'feature_bytes': 64 * 1024**2}}
    evidence.freeze(directory, 810, specification, inputs,
        references=[study.ORIGINAL / 'plan.json', study.ORIGINAL / 'extraction.receipt.json',
                    study.ORIGINAL / 'predictions.receipt.json', study.ORIGINAL / 'pcm-audit.json',
                    study.ORIGINAL / 'pcm-audit.receipt.json', ROOT / 'requirements.lock'],
        documents=[DOCUMENT, WORKER, LAB.parent / 'investigations/chroma_810.py'])
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json')}), flush=True)


def extract(directory):
    if (directory / 'extraction.receipt.json').exists():
        check_receipt(directory, 'extraction.receipt.json')
        return
    rows = load(directory / 'inputs/baseline-rows.json')['rows']
    original = arrays(directory / 'inputs/baseline-features.npz')
    audit = load(study.ORIGINAL / 'pcm-audit.json')['intervals']
    checksums = {(r['id'], r['start']): r['pcm_16000_sha256'] for r in audit}
    features = np.zeros((*original['musicnn'].shape[:2], 72))
    env = {**os.environ, **{k: '1' for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                                           'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS')},
           'NUMBA_CACHE_DIR': str(ROOT / 'numba-cache')}
    started = time.monotonic()
    profiles, replay = [], []
    with TemporaryDirectory(prefix='chroma-pcm-', dir=directory) as temporary, (directory / 'worker.log').open('a') as log:
        worker = subprocess.Popen([str(PYTHON), '-B', str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, text=True, env=env, bufsize=1)
        try:
            runtime = response(worker, 'READY ')
            checked = set()
            for i, row in enumerate(rows):
                if row['audio_sha256'] not in checked:
                    if digest(row['audio']) != row['audio_sha256']:
                        raise ValueError('Original audio changed')
                    checked.add(row['audio_sha256'])
                for j, start in enumerate(row['chunk_starts']):
                    if time.monotonic() - started > 3600:
                        raise RuntimeError('Extraction deadline exceeded')
                    signal = Path(temporary) / 'interval.f32'
                    pcm(row['audio'], start, 16000, 1, signal)
                    checksum = digest(signal)
                    if signal.stat().st_size != 640000 or checksum != checksums[(row['id'], start)]:
                        raise ValueError('Original interval PCM parity failed')
                    worker.stdin.write(json.dumps({'id': row['id'], 'pcm': str(signal)}) + '\n')
                    worker.stdin.flush()
                    result = response(worker, 'RESULT ')
                    if result['id'] != row['id']:
                        raise ValueError('Worker row order changed')
                    features[i, j] = result.pop('embedding')
                    profiles.append({**result, 'start': start, 'pcm_sha256': checksum})
                print(json.dumps({'chroma_extracted': i + 1, 'source': row['source']}), flush=True)
            pcm(rows[0]['audio'], rows[0]['chunk_starts'][0], 16000, 1, signal)
            for _ in range(3):
                worker.stdin.write(json.dumps({'id': 'replay', 'pcm': str(signal)}) + '\n')
                worker.stdin.flush()
                result = response(worker, 'RESULT ')
                replay.append({'seconds': result['seconds'], 'maximum_error':
                    float(np.max(np.abs(features[0, 0] - result['embedding'])))})
            if max(r['maximum_error'] for r in replay) > 1e-6:
                raise ValueError('Chroma replay failed')
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
    combined = np.concatenate([original['musicnn'], features], axis=2)
    np.savez_compressed(directory / 'features.npz', musicnn=original['musicnn'],
                        chroma=features, combined=combined, counts=original['counts'])
    size = (directory / 'features.npz').stat().st_size
    if size > 64 * 1024**2:
        raise RuntimeError('Feature storage budget exceeded')
    save(directory / 'extraction.json', {'rows': rows, 'audit': profiles, 'resources': {
        'seconds': time.monotonic() - started, 'runtime': runtime, 'feature_bytes': size,
        'peak_worker_rss_bytes': max(r['peak_rss_bytes'] for r in profiles), 'replay': replay,
        'environment_bytes': sum(p.stat().st_size for p in (ROOT / 'venv').rglob('*') if p.is_file())}})
    evidence.seal(directory, 'extraction', [directory / 'features.npz', directory / 'extraction.json'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['environment', 'freeze', 'extract', 'fit', 'evaluate', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'environment':
            environment()
        elif args.action == 'freeze':
            freeze(directory)
        elif args.action == 'verify':
            print(json.dumps(evidence.verify(directory, required_stages=['extraction', 'predictions', 'evaluation'])))
        else:
            evidence.validate(directory, execution=True)
            {'extract': extract, 'fit': study.fit, 'evaluate': study.evaluate}[args.action](directory)


if __name__ == '__main__':
    main()
