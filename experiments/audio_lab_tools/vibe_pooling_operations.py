"""Bounded operational rehearsal on a disposable test-library snapshot.

This exercises the product-format candidate, not the detached research recipes.
Its temporary qualification is confined to this process and private library.
Missing or deferred work remains explicit; this report never qualifies a model.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import resource
import threading
import time
import zipfile

import numpy as np

from yt_playlist.core import file_paths
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, vibe_joint_model as model, vibe_joint_preparation as preparation
from yt_playlist.rec import vibe_preparation
from yt_playlist.rec.progressive import Preparation
from yt_playlist.repos import vibe_models

ROOT = Path(__file__).resolve().parents[2]
OWNERS = ('recording_vibes', 'measurements', 'predictions', 'prediction_inputs',
          'prediction_artists', 'prediction_model_generations', 'prediction_provenance')


def process_io():
    """Linux process counters, including WAL/files and cache effects."""
    return {key: int(value) for line in Path('/proc/self/io').read_text().splitlines()
            for key, value in (line.split(':', 1),)}


def storage(store):
    objects = dict(store.conn.execute('SELECT s.name,sum(d.pgsize) FROM sqlite_schema s '
        'JOIN dbstat d ON d.name=s.name WHERE s.tbl_name IN ('
        + ','.join('?' for _ in OWNERS) + ') GROUP BY s.name', OWNERS))
    counts = {table: store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
              for table in OWNERS}
    files = {}
    for domain in ('vibe-models', 'vibe-provenance'):
        root = file_paths.directory(store.conn, domain)
        values = [path.stat().st_size for path in root.rglob('*.json') if path.is_file()]
        files[domain] = {'files': len(values), 'bytes': sum(values), 'largest': max(values, default=0)}
    database_files = {}
    for suffix in ('', '-wal', '-shm'):
        path = Path(str(store._db_path) + suffix)
        if path.exists():
            value = path.stat()
            database_files[path.name] = {'bytes': value.st_size, 'allocated_bytes': value.st_blocks * 512}
    return {'sql_objects': objects, 'sql_bytes': sum(objects.values()), 'rows': counts, 'fao': files,
            'database_files': database_files}


def summary(samples):
    return {'requests': len(samples), 'p50_ms': float(np.percentile(samples, 50) * 1000),
            'p95_ms': float(np.percentile(samples, 95) * 1000), 'max_ms': max(samples) * 1000}


def foreground(store, keys):
    """One identical bounded SQL serving workload in both phases."""
    with store.priority.request(), store.read_view() as reader:
        ranked = [reader.vibe_predictions.ranked(vibe, 100) for vibe in home_vibe.PRESETS]
        scores = reader.vibe_predictions.fit_scores(keys, 'relax')
        overlays = reader.vibe_predictions.overlay({key: {} for key in keys})
    return {'ranked': sum(map(len, ranked)), 'scores': len(scores), 'overlays': len(overlays)}


@contextmanager
def serving_guards(foreground_thread):
    original_fit, original_read = model.fit, vibe_models.read
    calls = {'foreground_fits': 0, 'foreground_model_reads': 0, 'fits': []}
    def fit(*args, **kwargs):
        if threading.get_ident() == foreground_thread:
            calls['foreground_fits'] += 1
            raise AssertionError('Foreground score read attempted fitting')
        started = time.monotonic()
        observation = {'vibe': args[1], 'originals': len(args[0]), 'outcome': 'deferred'}
        try:
            value = original_fit(*args, **kwargs)
            observation['outcome'] = 'ready' if value else 'empty'
            observation['accounted_memory_bytes'] = value['diagnostics']['accounted_memory_bytes'] if value else 0
            return value
        finally:
            observation['seconds'] = time.monotonic() - started
            calls['fits'].append(observation)
    def read(*args, **kwargs):
        if threading.get_ident() == foreground_thread:
            calls['foreground_model_reads'] += 1
            raise AssertionError('Foreground score read attempted loading a model')
        return original_read(*args, **kwargs)
    model.fit, vibe_models.read = fit, read
    try:
        yield calls
    finally:
        model.fit, vibe_models.read = original_fit, original_read


def phase(store, keys, candidate, *, seconds, turns, request_idle):
    preparation.QUALIFICATIONS = ({preparation.config_identity(): 'isolated-operational-rehearsal'}
                                 if candidate else {})
    started, samples, first_useful = time.monotonic(), [], None
    observations, progress = [], []
    first_cycle, first_score = None, None
    io_before = process_io()
    def background():
        with store.priority.background(check_stopping):
            return background_work()

    def background_work():
        nonlocal first_cycle, first_score
        # Same single background worker and refresh request in each phase.
        # Candidate fitting is the intended extra work, never a foreground call.
        with atomic_write(store):
            store.conn.execute("INSERT OR IGNORE INTO rec_vibe_pending(group_key) VALUES ('*')")
        worker = Preparation(store)
        try:
            for turn in range(turns):
                if time.monotonic() - started >= seconds:
                    return {'outcome': 'bounded-incomplete', 'turns': turn}
                at = time.monotonic()
                if (not preparation.due(store) and not store.vibe_predictions.pending()
                        and not store.prepared_content.has_pending() and not store.musical_profiles.pending()):
                    return {'outcome': 'complete', 'turns': turn}
                kind = 'model-eligible' if preparation.due(store) else 'content-and-scores'
                changed = worker.capture(1000)
                if first_score is None and candidate and store.conn.execute(
                        'SELECT 1 FROM prediction_model_generations LIMIT 1').fetchone():
                    first_score = time.monotonic() - started
                if first_cycle is None and candidate and all(
                        preparation.current(store, value := preparation.state(store, vibe))
                        and value.get('outcome') == 'ready' for vibe in preparation.VIBES):
                    first_cycle = time.monotonic() - started
                progress.append({'kind': kind, 'changed': bool(changed), 'seconds': time.monotonic() - at})
            return {'outcome': 'bounded-incomplete', 'turns': turns}
        finally:
            worker.close()
    with serving_guards(threading.get_ident()) as calls, ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(background)
        while not work.done() or len(samples) < 100:
            at = time.monotonic()
            observed = foreground(store, keys)
            samples.append(time.monotonic() - at)
            if first_useful is None and observed['ranked'] and observed['scores']:
                first_useful = time.monotonic() - started
            if observed not in observations:
                observations.append(observed)
            # Bound benchmark request pressure; both phases use the same cadence.
            time.sleep(request_idle)
        outcome = work.result()
    return {**outcome, 'wall_seconds': time.monotonic() - started,
            'foreground': summary(samples), 'first_existing_useful_seconds': first_useful,
            'first_ready_model_cycle_seconds': first_cycle,
            'first_generation_score_seconds': first_score,
            'process_io_delta': {key: value - io_before[key] for key, value in process_io().items()},
            'observed_content': observations, 'guards': calls, 'background_turns': progress,
            'model_states': {vibe: preparation.state(store, vibe) for vibe in preparation.VIBES},
            'score_work_remaining': bool(store.vibe_predictions.pending())}


def run(output, *, seconds=180., turns=1000, request_idle=.1):
    database = Path(os.environ.get('TUNECONSOLE_TEST_LIBRARY', ''))
    if (os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1' or database != Path('/library/state.db')
            or output.parent.resolve() != Path('/reports')):
        raise ValueError('Use scripts/test-library.py with a private writable snapshot and /reports output')
    original_qualification = preparation.QUALIFICATIONS
    started = time.monotonic()
    if request_idle < 0:
        raise ValueError('Request idle time must be nonnegative')
    report = {'candidate_selected': False, 'product_candidate': model.configuration(),
        'request_idle_seconds': request_idle,
        'priority': 'production request/background scopes; identical SQL workload and cadence in both phases',
        'dataset': 'private CoW snapshot selected by test-library manager',
        'limits': ['Warm existing prepared state; does not establish fresh-user startup.',
            'Product-format candidate only; detached metadata/paired recipes are not integrated.',
            'Single background worker and foreground caller; bounded SQL workload, not full Home HTTP.',
            'Baseline precedes candidate on one snapshot; cache/order effects remain possible.',
            'No independent backup/restore or full retention acceptance follows from these timings.'],
        'code': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            'experiments/audio_lab_tools/vibe_pooling_operations.py',
            'src/yt_playlist/rec/vibe_joint_model.py', 'src/yt_playlist/rec/vibe_joint_preparation.py',
            'src/yt_playlist/rec/progressive.py',
            'src/yt_playlist/core/priority.py',
            'src/yt_playlist/rec/vibe_preparation.py', 'src/yt_playlist/repos/vibe_predictions.py',
            'src/yt_playlist/repos/vibe_models.py', 'src/yt_playlist/core/vibe_model_files.py')}}
    with zipfile.ZipFile(output.parent / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, digest in report['code'].items():
            raw = (ROOT / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Operational source changed before archive')
            archive.writestr(name, raw)
    store = Store(database)
    try:
        store.init_schema()
        report['schema'] = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        # Real library identities, including own measurements and inferred scores.
        # This limits only the serving request; every original enters each fit.
        keys = [row[0] for row in store.conn.execute('SELECT DISTINCT r.identity_key '
            'FROM recordings r JOIN recording_vibes v ON v.recording_id=r.id ORDER BY r.id LIMIT 128')]
        if len(keys) < 20:
            raise ValueError('Insufficient real prepared library content')
        report['serving_keys'] = len(keys)
        report['before'] = storage(store)
        report['baseline'] = phase(store, keys, False, seconds=seconds, turns=turns, request_idle=request_idle)
        report['after_baseline'] = storage(store)
        report['candidate'] = phase(store, keys, True, seconds=seconds, turns=turns, request_idle=request_idle)
        report['after_candidate'] = storage(store)
        report['fit_original_counts'] = {vibe: len(store.vibe_predictions.joint_inputs(vibe, model.MAX_ROWS))
                                         for vibe in preparation.VIBES}
        report['generation_links'] = [dict(row) for row in store.conn.execute(
            'SELECT hex(generation) AS generation,count(*) AS predictions '
            'FROM prediction_model_generations GROUP BY generation')]
        report['foreign_key_failures'] = [tuple(row) for row in store.conn.execute('PRAGMA foreign_key_check')]
        baseline, candidate = report['baseline'], report['candidate']
        report['observed_foreground_limit'] = (candidate['foreground']['p95_ms']
            <= baseline['foreground']['p95_ms'] * 1.1 + 2)
        models_ready = all(value.get('outcome') == 'ready' for value in candidate['model_states'].values())
        report['foreground_point_gate'] = (report['observed_foreground_limit']
            if baseline['outcome'] == candidate['outcome'] == 'complete' and models_ready else None)
        report['peak_process_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        report['seconds'] = time.monotonic() - started
        report['operational_acceptance'] = False
        report['source_pins_unchanged'] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                                             for name, digest in report['code'].items())
        report['remaining'] = ['fresh-user first useful output', 'installed-format continuing writes',
            'actual-generation retention/collection and independent restore', 'refresh physical I/O/write amplification',
            'independent model-quality qualification and full Home request acceptance']
    finally:
        preparation.QUALIFICATIONS = original_qualification
        store.conn.close()
        # Save partial diagnostics even if a controlled rehearsal exposes a defect.
        output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps({key: report[key] for key in ('seconds', 'foreground_point_gate',
        'peak_process_rss_bytes', 'operational_acceptance')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--seconds', type=float, default=180.)
    parser.add_argument('--turns', type=int, default=1000)
    parser.add_argument('--request-idle', type=float, default=.1)
    args = parser.parse_args()
    run(args.output, seconds=args.seconds, turns=args.turns, request_idle=args.request_idle)
