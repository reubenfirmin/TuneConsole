"""Resumable exact-interval MusiCNN extraction for the November experiments."""
import json
import os
from pathlib import Path
import resource
import select
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
import time

import numpy as np

from ..datasets.dataset_windows import WORK, checked_record, content_hash, extraction_runtime, feature_vectors
from ..support.external_experiment import digest, pcm, probe, save
from ..focus.focus_experiment import load

RUNTIME_LOCK = Lock()

def provenance():
    # The historical snapshot helper rewrites one manifest with a fixed staging
    # filename. Initialize/check it serially before independent workers run.
    with RUNTIME_LOCK:
        runtime, worker, original = extraction_runtime('quality-round')
    stable = {k: v for k, v in original.items() if k not in ('plan_sha256', 'script_sha256', 'policy')}
    return runtime, worker, stable


class Extractor:
    def __init__(self, directory, seconds=7200, previous=()):
        self.directory, self.seconds = directory, seconds
        self.runtime, self.worker_path, self.provenance = provenance()
        self.started, self.new, self.reused = time.monotonic(), 0, 0
        self.durations, self.origins = {}, {}
        original = WORK / 'features.json'
        if original.exists():
            for row in load(original)['rows']:
                for chunk in row['chunks']:
                    self.origins[row['audio_sha256'], chunk['start']] = chunk
        self.cached = {}
        for parent in previous:
            for path in sorted(parent.glob('**/chunks/*.json')):
                record = load(path)
                if record['input']['provenance'] == self.provenance:
                    self.cached[record['input']['audio_sha256'], record['input']['start']] = path

    def __enter__(self):
        self.scratch = TemporaryDirectory(prefix='pcm-', dir=self.directory)
        self.log = (self.directory / 'extractor.log').open('a')
        env = {**os.environ, 'EXTERNAL_TFJS_NATIVE': '1', 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1'}
        self.worker = subprocess.Popen(['nice', '-n', '10', 'node', str(self.worker_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1, env=env)
        return self

    def chunk(self, row, start):
        if time.monotonic() - self.started > self.seconds:
            raise ValueError('Frozen extraction wall-clock budget exceeded')
        path = Path(row['audio'])
        checksum = row['audio_sha256']
        if checksum not in self.durations:
            if digest(path) != checksum:
                raise ValueError('Audio checksum changed')
            self.durations[checksum] = probe(path)[0]
        if start < 0 or start + 10 > self.durations[checksum]:
            raise ValueError('Audio does not cover the exact ten-second chunk')
        identity = {'audio_sha256': checksum, 'start': start, 'seconds': 10, 'provenance': self.provenance}
        cache = self.directory / 'chunks' / (content_hash(identity) + '.json')
        if cache.exists():
            record = load(cache)
            if record['input'] != identity or content_hash(record['result']) != record['result_sha256']:
                raise ValueError('New feature cache changed')
            if record.get('origin') and digest(record['origin']['path']) != record['origin']['sha256']:
                raise ValueError('Reused feature origin changed')
            return feature_vectors(record['result']), cache
        prior = self.cached.get((checksum, start))
        origin = self.origins.get((checksum, start))
        if prior:
            record = load(prior)
            if record['input'] != identity or content_hash(record['result']) != record['result_sha256']:
                raise ValueError('Prior feature cache changed')
            result = record['result']
            origin = {'path': str(prior), 'sha256': digest(prior)}
            self.reused += 1
        elif origin:
            if digest(origin['path']) != origin['sha256']:
                raise ValueError('Original feature file changed')
            raw = load(origin['path'])
            record = checked_record(origin['path'], raw['input'])
            old = {k: v for k, v in raw['input']['provenance'].items() if k not in ('plan_sha256', 'script_sha256', 'policy')}
            if old != self.provenance:
                raise ValueError('Reused extractor provenance differs')
            result = record['result']
            self.reused += 1
        else:
            signal = Path(self.scratch.name) / 'model.f32'
            pcm(path, start, 16000, 1, signal)
            self.worker.stdin.write(json.dumps({'id': row['id'], 'model_pcm': str(signal)}) + '\n')
            self.worker.stdin.flush()
            while True:
                if not select.select([self.worker.stdout], [], [], 120)[0]:
                    raise RuntimeError('Feature worker exceeded the 120-second deadline')
                line = self.worker.stdout.readline()
                if not line:
                    raise RuntimeError('Feature worker exited; inspect extractor.log')
                if line.startswith('RESULT '):
                    result = json.loads(line.removeprefix('RESULT '))
                    if result.get('id') != row['id']:
                        raise RuntimeError('Feature worker returned a different row')
                    break
            self.new += 1
        vector = feature_vectors(result)
        record = {'input': identity, 'result': result, 'result_sha256': content_hash(result)}
        if origin:
            record['origin'] = {'path': origin['path'], 'sha256': origin['sha256']}
        save(cache, record)
        return vector, cache

    def __exit__(self, kind, value, trace):
        try:
            if kind:
                self.worker.terminate()
            else:
                self.worker.stdin.close()
            try:
                self.worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=10)
        finally:
            self.log.close()
            self.scratch.cleanup()
        self.resources = {'new_chunks': self.new, 'reused_chunks': self.reused,
            'elapsed_seconds': time.monotonic() - self.started,
            'child_peak_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024}


def recording_weights(ids):
    unique, inverse, counts = np.unique(ids, return_inverse=True, return_counts=True)
    return 1. / counts[inverse]
