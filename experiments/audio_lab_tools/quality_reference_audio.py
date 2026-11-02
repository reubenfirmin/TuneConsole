"""Previously validated C++ MusiCNN extraction, retaining exact interval identity."""
import json
import os
from pathlib import Path
import resource
import select
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import numpy as np

from .dataset_windows import content_hash
from .external_experiment import LAB, digest, pcm, probe, save
from .focus_experiment import load
from . import preprocessing_native

GRAPH = LAB / 'local/external-20261028/checkpoint-1/reference-pipeline/musicnn.pb'
PACKAGE = LAB / 'local/reference-python/essentia_tensorflow-2.1b6.dev1438.dist-info'


def provenance():
    return {'backend': 'essentia-tensorflow', 'graph_sha256': digest(GRAPH),
        'worker_sha256': digest(preprocessing_native.__file__), 'package_metadata_sha256': digest(PACKAGE / 'METADATA'),
        'package_record_sha256': digest(PACKAGE / 'RECORD'),
        'decoder': subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0],
        'patch_policy': '512 frame, 256 hop, three full nonoverlapping 187-frame patches, mean preactivation 200-vector',
        'accepted_feature_error_limit': .0005, 'prior_validation': 'PREPROCESSING_QUALITY_424.md'}


def response(worker, prefix):
    if not select.select([worker.stdout], [], [], 120)[0]:
        raise RuntimeError('Reference worker exceeded 120-second deadline')
    line = worker.stdout.readline()
    if not line.startswith(prefix):
        raise RuntimeError('Reference worker exited or returned invalid protocol: ' + line[:200])
    return json.loads(line[len(prefix):])


class Extractor:
    def __init__(self, directory, seconds=7200):
        self.directory, self.seconds, self.provenance = directory, seconds, provenance()
        self.started, self.new, self.reused = time.monotonic(), 0, 0
        self.durations = {}

    def __enter__(self):
        self.scratch = TemporaryDirectory(prefix='pcm-', dir=self.directory)
        self.log = (self.directory / 'extractor.log').open('a')
        env = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1', 'OMP_NUM_THREADS': '1'}
        self.worker = subprocess.Popen([sys.executable, '-B', '-m', 'experiments.audio_lab_tools.preprocessing_native',
            '--graph', str(GRAPH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, bufsize=1, env=env)
        try:
            self.runtime = response(self.worker, 'READY ')
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def chunk(self, row, start):
        if time.monotonic() - self.started > self.seconds:
            raise RuntimeError('Frozen extraction time budget exceeded')
        path, checksum = Path(row['audio']), row['audio_sha256']
        if checksum not in self.durations:
            if digest(path) != checksum:
                raise RuntimeError('Audio checksum changed')
            self.durations[checksum] = probe(path)[0]
        if start < 0 or start + 10 > self.durations[checksum]:
            raise ValueError('Audio does not cover the exact ten-second interval')
        identity = {'audio_sha256': checksum, 'start': start, 'seconds': 10, 'provenance': self.provenance}
        cache = self.directory / 'chunks' / (content_hash(identity) + '.json')
        if cache.exists():
            record = load(cache)
            if record['input'] != identity or content_hash(record['result']) != record['result_sha256']:
                raise RuntimeError('Feature cache changed')
            result = record['result']
            self.reused += 1
        else:
            signal = Path(self.scratch.name) / 'model.f32'
            pcm(path, start, 16000, 1, signal)
            self.worker.stdin.write(json.dumps({'id': row['id'], 'pcm': str(signal)}) + '\n')
            self.worker.stdin.flush()
            result = response(self.worker, 'RESULT ')
            if result['id'] != row['id'] or result['patches'] != 3:
                raise RuntimeError('Reference worker returned a different interval or patch policy')
            save(cache, {'input': identity, 'result': result, 'result_sha256': content_hash(result)})
            self.new += 1
        values = np.asarray(result['embedding'])
        if values.shape != (200,) or not np.isfinite(values).all():
            raise RuntimeError('Invalid reference embedding')
        return values, cache

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
            'child_peak_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024,
            'runtime': getattr(self, 'runtime', None)}
