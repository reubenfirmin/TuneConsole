"""Freeze and evaluate bounded, time-aligned pilots from newly acquired audio.

Target labels never select the excerpt, source hyperparameters or source models.
These pilots precede any training on ManyMusic or Memo2496.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .. import TOOLS
from ..support.external_experiment import DATA, LAB, OUT, ROOT, digest, pcm, probe, save, stamp
from ..support.runtime_snapshot import ensure as frozen_runtime

WORK = OUT / 'checkpoint-1/new-corpora'
TARGETS = ['valence', 'arousal']
POLICY = 'new-audio-pilot-1'
SOURCES = ('pmemo', 'pmemo-dynamic', 'deam', 'deam-dynamic')
GENERIC_ARTIST_CREDITS = {'variousartists', 'various', 'unknownartist', 'unknown', 'na', 'none'}
DEPENDENCIES = (
    *(str(path.relative_to(ROOT)) for path in sorted(TOOLS.rglob('__init__.py'))),
    'experiments/audio_lab_tools/support/external_experiment.py',
    'experiments/audio_lab_tools/training/external_training.py',
    'experiments/audio_lab_tools/training/external_learners.py',
    'experiments/audio_lab_tools/datasets/external_secondary.py',
    'experiments/audio_lab_tools/studies/external_checkpoint.py',
    'experiments/audio_lab_tools/support/runtime_snapshot.py',
    'src/yt_playlist/core/audio_embedding.py',
    'scripts/audio_model/build_runtime.py',
)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def normalize_targets(values):
    if len(values) != 2 or any(not math.isfinite(value) or not -1 <= value <= 1 for value in values):
        raise ValueError('Expected two finite signed VA coordinates')
    return [(value + 1) / 2 for value in values]


def trajectory_mean(coordinates, start, end):
    """Require every one-second annotation in the actual half-open interval."""
    selected = []
    for name in TARGETS:
        series = coordinates[name]
        times = [point[0] for point in series]
        if len(times) != len(set(times)):
            raise ValueError('Duplicate trajectory timestamp')
        if any(not math.isfinite(value) or not -1 <= value <= 1 for _, value in series):
            raise ValueError('Invalid trajectory coordinate')
        by_time = {point[0]: point[1] for point in series}
        expected = list(range(start, end))
        if not all(second in by_time for second in expected):
            raise ValueError('Incomplete annotation coverage')
        selected.append(float(np.mean([by_time[second] for second in expected])))
    return normalize_targets(selected)


def many_music_rows(rows):
    by_recording = defaultdict(list)
    for row in rows:
        by_recording[row['recording_id']].append(row)
    selected = []
    for key, choices in sorted(by_recording.items()):
        # Only the recording/window identity enters selection. Neither targets,
        # model outputs nor the number of agreeing listeners break ties.
        row = min(choices, key=lambda item: content_hash([POLICY, item['id']]))
        length = row['annotation_seconds']
        if length != 30:
            raise ValueError('ManyMusic pilot requires the released 30-second window')
        start = row['annotation_start']
        native = dict(zip(row['target_names'], row['targets']))
        selected.append({**row, 'target_names': TARGETS,
            'targets': normalize_targets([native[name] for name in TARGETS]),
            'signed_native_targets': [native[name] for name in TARGETS],
            'native_scale': [-1, 1], 'model_target_scale': [0, 1],
            'chunk_starts': [start + offset for offset in (0, 10, 20)],
            'covered_interval': [start, start + 30], 'recording_windows_available': len(choices),
            'subset': 'manymusic-generated' if row['synthetic_audio'] else 'manymusic-natural'})
    return selected


def memo_rows(rows):
    selected, excluded = [], []
    for row in rows:
        try:
            targets = trajectory_mean(row['coordinates'], 15, 75)
            chunk_targets = [trajectory_mean(row['coordinates'], start, start + 10)
                             for start in range(15, 75, 10)]
        except ValueError as error:
            excluded.append({'id': row['id'], 'reason': str(error)})
            continue
        selected.append({**row, 'recording_id': row['id'], 'target_names': TARGETS,
            'targets': targets, 'signed_native_targets': [value * 2 - 1 for value in targets],
            'model_target_scale': [0, 1], 'chunk_targets': chunk_targets,
            'chunk_starts': list(range(15, 75, 10)), 'covered_interval': [15, 75],
            'annotation_unit': 'expert_trajectory_mean_over_complete_15_to_75_second_interval',
            'subset': 'memo2496-pilot'})
    return selected, excluded


def plan():
    inputs = {
        'manymusic': DATA / 'discovery/manymusic/human-window-index.json',
        'manymusic_provenance': DATA / 'discovery/manymusic/audio-provenance-index.json',
        'memo2496': DATA / 'discovery/memo2496/pilot-human-index.json',
    }
    audits = {name: json.loads(path.read_text()) for name, path in inputs.items()}
    credits = audits['manymusic_provenance']
    if credits['human_index_sha256'] != digest(inputs['manymusic']):
        raise ValueError('ManyMusic attribution is stale; rerun the provenance audit')
    by_recording = {row['recording_id']: row for row in credits['rows']}
    many = many_music_rows(audits['manymusic']['rows'])
    for row in many:
        if row['synthetic_audio']:
            continue
        credit = by_recording[row['recording_id']]
        if credit['audio_sha256'] != row['audio_sha256'] or credit['artist_id'] != row['artist_id']:
            raise ValueError('ManyMusic attribution and audio identity differ')
        row.update(artist=credit['artist'], title=credit['title'],
                   source_url=credit['source_url'], license_url=credit['license_url'],
                   publisher_attribution=credit['publisher_attribution'])
    memo, excluded = memo_rows(audits['memo2496']['rows'])
    rows = many + memo
    if any(not row.get('audio_available') or not row.get('audio_sha256') for row in rows):
        raise ValueError('Complete verified audio acquisition is required before freezing the pilot')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate pilot identity')
    payload = {'policy': POLICY, 'input_sha256': {name: digest(path) for name, path in inputs.items()},
        'code_sha256': digest(__file__),
        'dependency_sha256': {name: digest(ROOT / name) for name in DEPENDENCIES},
        'rows': rows, 'annotation_exclusions': excluded,
        'selection': 'One exact 30-second ManyMusic window per rated recording, selected by the lowest SHA256(policy, window identity), independent of targets and rater count. Memo uses the previously frozen 64-file pilot and complete annotations for 15 <= time < 75 seconds. Missing coverage is excluded, never imputed or replaced.',
        'evaluation': 'Keep all ten-second chunks of each exact rated interval. Compare prediction on the mean embedding with the mean of chunk predictions. Report both fixed methods without choosing on target outcomes. Source mean, neutral midpoint and source-only-selected ridge models provide controls. No target fitting or calibration.',
        'sources': list(SOURCES), 'source_model': 'Ridge on the 200-dimensional MusiCNN embedding; regularization and preprocessing selected on three grouped source-only folds.',
        'limitations': ['This is a bounded recording-balanced pipeline/transfer pilot, not all 1695 ManyMusic windows or the full Memo2496 benchmark.',
            'A selected ManyMusic window often has one listener; repeated windows are not independent tracks.',
            'Natural and generated audio are separate evaluation strata; generated prompts are not targets.',
            'Exact normalized artist credits supplement Jamendo IDs for conservative overlap exclusions. Name collisions, unknown aliases, re-encodings and upstream model-training overlap remain unresolved; Memo artist identities are unavailable.',
            'Missing real audio coverage rejects the whole rated interval; no silence padding enters the music features.',
            'Ten-second chunk features do not reproduce a model trained on continuous 30/60-second inputs.']}
    key = content_hash(payload)
    path = WORK / 'plans' / (key + '.json')
    if path.exists():
        if json.loads(path.read_text()) != payload:
            raise ValueError('Immutable pilot plan changed')
    else:
        save(path, payload)
    save(WORK / 'current-plan.json', {'created_at': stamp(), 'path': str(path), 'sha256': digest(path)})
    counts = {subset: sum(row['subset'] == subset for row in rows)
              for subset in sorted({row['subset'] for row in rows})}
    print(json.dumps({'pilot_plan': str(path), 'recordings': counts,
        'chunks': sum(len(row['chunk_starts']) for row in rows), 'annotation_exclusions': excluded}), flush=True)
    return payload


def load_plan():
    index = json.loads((WORK / 'current-plan.json').read_text())
    path = Path(index['path'])
    if digest(path) != index['sha256']:
        raise ValueError('Pilot plan changed')
    value = json.loads(path.read_text())
    if value['code_sha256'] != digest(__file__):
        raise ValueError('Pilot code changed; freeze a new plan before extraction or evaluation')
    if any(digest(ROOT / name) != expected for name, expected in value['dependency_sha256'].items()):
        raise ValueError('Pilot dependency changed; freeze a new plan before extraction or evaluation')
    return value, index['sha256']


def check_interval(row, duration):
    start, end = row['covered_interval']
    starts = row['chunk_starts']
    # Released decimal timestamps do not have exact binary representations.
    # This tolerance only checks the chunk layout; actual audio coverage remains
    # strict. It is much smaller than the decoder's microsecond seek precision.
    close = lambda left, right: math.isclose(left, right, rel_tol=0, abs_tol=1e-8)
    if (not all(math.isfinite(value) for value in [duration, start, end, *starts])
            or start < 0 or end > duration or end <= start
            or not starts or not close(starts[0], start) or not close(starts[-1] + 10, end)
            or any(not close(right - left, 10) for left, right in zip(starts, starts[1:]))):
        raise ValueError('Real audio does not cover the complete consecutive rated interval')


def feature_vectors(result):
    if result.get('error') or result.get('research_backend') != 'tensorflow':
        raise ValueError('Unexpected inference result or backend')
    if result.get('sample_count') != 160000:
        raise ValueError('Expected exactly ten seconds of 16 kHz model audio')
    embedding = np.asarray(result['prediction']['embedding']['values'], dtype=float)
    if embedding.shape != (200,) or not np.isfinite(embedding).all():
        raise ValueError('Invalid embedding')
    return embedding


def embedding_signature(result):
    from yt_playlist.core.audio_embedding import signature
    prediction = result['prediction']
    return signature({**prediction, 'artifact_hashes': {
        item['path']: item['sha256'] for item in prediction['bundle']['files']}})


def extraction_runtime(plan_hash):
    runtime, worker_path = frozen_runtime()
    # The features are kept separate from the original extractor caches. They
    # have their own window policy and recorded runtime/backend provenance.
    provenance = {'plan_sha256': plan_hash, 'script_sha256': digest(__file__),
        'runtime_manifest_sha256': digest(runtime.parent / 'manifest.json'),
        'worker_sha256': digest(worker_path),
        'vendor_bundle_sha256': digest(runtime / 'vendor/bundle.json'),
        'native_package_sha256': digest(LAB / 'local/node/package-lock.json'),
        'decoder': subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0],
        'backend': 'tensorflow-native', 'policy': POLICY}
    return runtime, worker_path, provenance


def checked_record(path, expected_input):
    record = json.loads(Path(path).read_text())
    if record['input'] != expected_input or content_hash(record['result']) != record['result_sha256']:
        raise ValueError('Chunk feature cache changed')
    if 'reused_from' in record:
        origin = record['reused_from']
        if digest(origin['path']) != origin['sha256']:
            raise ValueError('Original reused chunk changed')
        original = json.loads(Path(origin['path']).read_text())
        original_input = original['input']
        if (content_hash(original['result']) != original['result_sha256']
                or original['result_sha256'] != record['result_sha256']
                or any(original_input[key] != expected_input[key] for key in ('audio_sha256', 'start', 'seconds'))
                or {key: value for key, value in original_input['provenance'].items()
                    if key not in ('plan_sha256', 'script_sha256')}
                   != {key: value for key, value in expected_input['provenance'].items()
                       if key not in ('plan_sha256', 'script_sha256')}):
            raise ValueError('Reused chunk runtime, input or result differs')
    feature_vectors(record['result'])
    return record


def repair_intervals(lifetime):
    """Preserve the faulty 325-row run and revalidate its immutable features."""
    old_index = json.loads((WORK / 'current-plan.json').read_text())
    old_plan_path = Path(old_index['path'])
    if digest(old_plan_path) != old_index['sha256']:
        raise ValueError('Original pilot plan changed')
    old_plan = json.loads(old_plan_path.read_text())
    if old_plan['code_sha256'] != '3c1fc9125e25abf697fb7419a343507e567a1a052c7297fb53cc2621ad02eac3':
        raise ValueError('Repair applies only to the known floating-point guard revision')
    if any(digest(ROOT / name) != expected for name, expected in old_plan['dependency_sha256'].items()):
        raise ValueError('Original extraction dependencies changed')
    old_features = json.loads((WORK / 'features.json').read_text())
    if (old_features['state'] != 'complete'
            or old_features['provenance']['plan_sha256'] != old_index['sha256']
            or old_features['provenance']['script_sha256'] != old_plan['code_sha256']):
        raise ValueError('Original pilot extraction does not match its plan')
    expected = {row['id']: row for row in old_plan['rows']}
    accounted = [row['id'] for row in old_features['rows'] + old_features['errors']]
    if len(accounted) != len(set(accounted)) or set(accounted) != set(expected):
        raise ValueError('Original pilot accounting changed')
    _, _, provenance = extraction_runtime('pending')
    runtime_keys = set(provenance) - {'plan_sha256', 'script_sha256'}
    if any(old_features['provenance'][key] != provenance[key] for key in runtime_keys):
        raise ValueError('Original extraction runtime differs; cache reuse forbidden')
    verified = []
    for row in old_features['rows']:
        if any(row.get(key) != value for key, value in expected[row['id']].items()):
            raise ValueError('Original extracted row differs from plan')
        check_interval(row, row['duration'])
        if [chunk['start'] for chunk in row['chunks']] != row['chunk_starts']:
            raise ValueError('Original chunk coverage differs from plan')
        for chunk in row['chunks']:
            if chunk['seconds'] != 10 or digest(chunk['path']) != chunk['sha256']:
                raise ValueError('Original chunk file changed')
            key_data = {'audio_sha256': row['audio_sha256'], 'start': chunk['start'],
                        'seconds': 10, 'provenance': old_features['provenance']}
            verified.append((chunk, checked_record(chunk['path'], key_data)))
    archive = WORK / 'archives/float-interval-1'
    if archive.exists():
        raise ValueError('Repair archive already exists; inspect it before another migration')
    archive.mkdir(parents=True)
    for name in ('current-plan.json', 'features.json', 'transfer-report.json', 'overlap-audit.json',
                 'campaign-status.json', 'models', 'predictions'):
        path = WORK / name
        if path.is_dir():
            shutil.copytree(path, archive / name)
        elif path.exists():
            shutil.copyfile(path, archive / name)
    save(archive / 'manifest.json', {'created_at': stamp(), 'reason': 'Exact floating-point equality falsely excluded 21 complete intervals.',
        'files': {str(path.relative_to(archive)): digest(path) for path in archive.rglob('*') if path.is_file()}})
    new_plan = plan()
    if ({key: value for key, value in new_plan.items() if key != 'code_sha256'}
            != {key: value for key, value in old_plan.items() if key != 'code_sha256'}):
        raise ValueError('Repair changed pilot inputs or selection; do not reuse features')
    _, new_plan_hash = load_plan()
    _, _, provenance = extraction_runtime(new_plan_hash)
    for chunk, record in verified:
        key_data = {**record['input'], 'provenance': provenance}
        path = WORK / 'features' / (content_hash(key_data) + '.json')
        save(path, {**record, 'input': key_data,
            'reused_from': {'path': chunk['path'], 'sha256': chunk['sha256']}})
        checked_record(path, key_data)
    save(WORK / 'interval-repair.json', {'created_at': stamp(), 'archive': str(archive),
        'archive_manifest_sha256': digest(archive / 'manifest.json'), 'old_plan_sha256': old_index['sha256'],
        'new_plan_sha256': new_plan_hash, 'reused_chunks': len(verified), 'script_sha256': digest(__file__)})
    extract(lifetime)
    transfer()


def extract(lifetime):
    payload, plan_hash = load_plan()
    runtime, worker_path, provenance = extraction_runtime(plan_hash)
    cache = WORK / 'features'
    pcm_root = WORK / 'pcm'
    pcm_root.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'EXTERNAL_TFJS_NATIVE': '1',
        'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1'}
    successes, errors = [], []
    started = time.monotonic()
    with (WORK / 'worker.log').open('a') as log:
        worker = subprocess.Popen(['nice', '-n', '10', 'node', str(worker_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, bufsize=1, env=environment, pass_fds=() if lifetime is None else (lifetime,))
        progress = {'created_at': stamp(), 'state': 'running', 'provenance': provenance,
                    'rows': successes, 'errors': errors, 'total': len(payload['rows'])}
        save(WORK / 'features.json', progress)
        try:
            for row in payload['rows']:
                try:
                    if digest(row['audio']) != row['audio_sha256']:
                        raise ValueError('Acquired audio changed')
                    duration, _ = probe(row['audio'], lifetime_fd=lifetime)
                    check_interval(row, duration)
                    features = []
                    for start in row['chunk_starts']:
                        key_data = {'audio_sha256': row['audio_sha256'], 'start': start,
                                    'seconds': 10, 'provenance': provenance}
                        path = cache / (content_hash(key_data) + '.json')
                        if path.exists():
                            record = checked_record(path, key_data)
                            result = record['result']
                        else:
                            signal = pcm_root / 'model.f32'
                            pcm(row['audio'], start, 16000, 1, signal, lifetime_fd=lifetime)
                            worker.stdin.write(json.dumps({'id': row['id'], 'model_pcm': str(signal.resolve())}) + '\n')
                            worker.stdin.flush()
                            while True:
                                if not select.select([worker.stdout], [], [], 120)[0]:
                                    raise RuntimeError('Chunk worker exceeded 120-second deadline')
                                line = worker.stdout.readline()
                                if not line:
                                    raise RuntimeError('Chunk worker exited')
                                if line.startswith('RESULT '):
                                    result = json.loads(line.removeprefix('RESULT '))
                                    if result.get('id') != row['id']:
                                        raise RuntimeError('Chunk worker returned a different recording identity')
                                    break
                            feature_vectors(result)
                            record = {'input': key_data, 'result': result, 'result_sha256': content_hash(result)}
                            save(path, record)
                        feature_vectors(result)
                        features.append({'start': start, 'seconds': 10, 'path': str(path), 'sha256': digest(path)})
                    successes.append({**row, 'duration': duration, 'chunks': features})
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    errors.append({'id': row['id'], 'error': str(error)})
                progress = {'created_at': stamp(), 'state': 'running', 'provenance': provenance, 'rows': successes,
                            'errors': errors, 'total': len(payload['rows'])}
                save(WORK / 'features.json', progress)
                print(json.dumps({'aligned_audio_recordings': len(successes), 'errors': len(errors),
                    'total': len(payload['rows']), 'elapsed_seconds': round(time.monotonic() - started, 1)}), flush=True)
            save(WORK / 'features.json', {**progress, 'state': 'complete'})
        except BaseException as error:
            save(WORK / 'features.json', {**progress, 'state': 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                                         'fatal_error': repr(error)})
            worker.terminate()
            raise
        finally:
            try:
                worker.stdin.close()
            except BrokenPipeError:
                pass
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()


def target_features():
    payload, plan_hash = load_plan()
    path = WORK / 'features.json'
    extracted = json.loads(path.read_text())
    if extracted.get('state') != 'complete' or extracted['provenance']['plan_sha256'] != plan_hash:
        raise ValueError('Current pilot extraction is not complete')
    rows, errors = extracted['rows'], extracted['errors']
    ids = [row['id'] for row in rows] + [row['id'] for row in errors]
    if len(ids) != len(set(ids)) or set(ids) != {row['id'] for row in payload['rows']}:
        raise ValueError('Pilot extraction does not account for every planned recording')
    vectors, signatures = {}, set()
    expected = {row['id']: row for row in payload['rows']}
    for row in rows:
        if any(row.get(key) != value for key, value in expected[row['id']].items()):
            raise ValueError('Extracted row differs from the frozen pilot plan')
        check_interval(row, row['duration'])
        if [chunk['start'] for chunk in row['chunks']] != row['chunk_starts']:
            raise ValueError('Incomplete ordered chunk coverage')
        values = []
        for chunk in row['chunks']:
            if chunk['seconds'] != 10 or digest(chunk['path']) != chunk['sha256']:
                raise ValueError('Pilot chunk file changed')
            record = checked_record(chunk['path'], {'audio_sha256': row['audio_sha256'],
                'start': chunk['start'], 'seconds': 10, 'provenance': extracted['provenance']})
            values.append(feature_vectors(record['result']))
            signatures.add(embedding_signature(record['result']))
        vectors[row['id']] = np.asarray(values)
    if len(signatures) != 1:
        raise ValueError('Pilot embeddings do not share one model/weight signature')
    return rows, vectors, {'plan_sha256': plan_hash, 'features_sha256': digest(path),
                           'embedding_signature': next(iter(signatures)),
                           'extraction_provenance': extracted['provenance'], 'extraction_errors': errors}


def overlap_audit(target):
    """Detect known catalog identities as well as encoded-file matches."""
    from .external_secondary import artist_keys
    known = defaultdict(list)
    snapshots = {}
    target_hashes = {row['audio_sha256'] for row in target}
    by_hash = defaultdict(list)
    by_artist = defaultdict(list)
    unresolved = {}
    for row in target:
        by_hash[row['audio_sha256']].append(row['id'])
        names = artist_keys(row.get('artist', ''))
        if names & GENERIC_ARTIST_CREDITS:
            unresolved[row['id']] = {'artist': row['artist'], 'reason': 'Generic credit does not establish artist identity'}
        for name in names - GENERIC_ARTIST_CREDITS:
            by_artist[name].append(row['id'])
    saved_rows = {}
    for path in sorted(OUT.glob('*.features.json')):
        raw = path.read_bytes()
        source = json.loads(raw)
        snapshots[path.name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'rows': len(source.get('rows', []))}
        saved_rows[path.name] = source.get('rows', [])
        for row in source.get('rows', []):
            if row['audio_sha256'] in target_hashes:
                for key in by_hash[row['audio_sha256']]:
                    known[key].append({'source': path.stem, 'id': row['id'], 'match': 'encoded_audio_sha256'})
            for name in sorted(artist_keys(row.get('artist', '')) & by_artist.keys()):
                for key in by_artist[name]:
                    known[key].append({'source': path.stem, 'id': row['id'],
                                       'match': 'normalized_artist_credit', 'credit': name})
    # ManyMusic distributes a different encoding from our earlier Jamendo shards.
    # The official stable catalog IDs still connect these recordings/artists.
    def catalog_id(value, prefix):
        return str(value).removeprefix(prefix + '_')
    previous_rows = saved_rows.get('jamendo-human-4shards.features.json')
    if previous_rows is not None:
        track_ids = {catalog_id(row['id'], 'track') for row in previous_rows}
        artist_ids = {catalog_id(row['source_artist_id'], 'artist') for row in previous_rows}
        for row in target:
            if row['subset'] != 'manymusic-natural':
                continue
            recording = row['recording_id'].removeprefix('manymusic:')
            for matched, kind in ((recording in track_ids, 'jamendo_recording_id'),
                                  (catalog_id(row['artist_id'], 'artist') in artist_ids, 'jamendo_artist_id')):
                if matched:
                    known[row['id']].append({'source': 'jamendo-human-4shards', 'match': kind})
    return {'known_corpus_overlaps': dict(known), 'unresolved_artist_credits': unresolved,
            'feature_manifests': snapshots,
            'policy': 'Primary slice conservatively excludes encoded matches, normalized artist-credit matches and known Jamendo recording/artist overlap with all earlier saved corpora. Generic credits cannot establish identity; other name matches can be collisions. All-audio results are a separate descriptive slice. Unknown artist aliases, edits and upstream backbone pretraining remain unresolved.'}


def audit():
    payload, plan_hash = load_plan()
    result = overlap_audit(payload['rows'])
    result.update(created_at=stamp(), plan_sha256=plan_hash,
                  scope='Metadata-only preview against currently saved feature manifests; extraction may still be incomplete. Transfer rebuilds this audit after acquiring the machine lock.')
    path = WORK / 'overlap-previews' / (content_hash(result) + '.json')
    save(path, result)
    counts = {}
    for subset in sorted({row['subset'] for row in payload['rows']}):
        rows = [row for row in payload['rows'] if row['subset'] == subset]
        counts[subset] = {'planned': len(rows), 'known_overlap': sum(row['id'] in result['known_corpus_overlaps'] for row in rows)}
    print(json.dumps({'pilot_overlap_preview': str(path), 'recordings': counts}), flush=True)
    return result


def transfer():
    from ..studies.external_checkpoint import require_complete_corpus
    from ..training.external_learners import serializable
    from ..training.external_training import (bootstrap_improvement, choose_alpha, fit_path,
                                             metrics, predict, validation_groups)
    rows, vectors, provenance = target_features()
    overlap = overlap_audit(rows)
    save(WORK / 'overlap-audit.json', overlap)
    report = {'created_at': stamp(), 'provenance': provenance, 'overlaps': overlap,
        'script_sha256': digest(__file__), 'directions': {}, 'target_names': TARGETS, 'metric_scale': [0, 1],
        'policy': 'All fits, imputation, scaling and ridge selection use only the named source corpus. Target labels are used only for evaluation. Both fixed chunk aggregation policies are reported; neither is selected using target results.',
        'limitations': load_plan()[0]['limitations']}
    for source in SOURCES:
        base = source.removesuffix('-dynamic')
        require_complete_corpus(base)
        path = OUT / (source + '.features.json')
        if source.endswith('-dynamic'):
            temporal = json.loads(path.read_text())['temporal_provenance']
            if temporal['source_features_sha256'] != digest(OUT / (base + '.features.json')):
                raise ValueError('Temporal source features are stale: ' + source)
        source_data = json.loads(path.read_text())
        source_rows = source_data['rows']
        names = source_rows[0]['target_names']
        if any(row['target_names'] != names for row in source_rows):
            raise ValueError('Source target order differs between rows')
        y = np.asarray([row['targets'] for row in source_rows], dtype=float)[:, [names.index(name) for name in TARGETS]]
        source_vectors, feature_hashes = [], {}
        for row in source_rows:
            result = json.loads(Path(row['feature_file']).read_text())
            if embedding_signature(result) != provenance['embedding_signature']:
                raise ValueError('Source and target embeddings use different model/weight signatures')
            source_vectors.append(feature_vectors(result))
            feature_hashes[row['feature_file']] = digest(row['feature_file'])
        source_provenance = {'source_features_sha256': digest(path), 'feature_file_sha256': feature_hashes,
                             'embedding_signature': provenance['embedding_signature']}
        # Source exclusion uses target identities, never target judgments.
        hashes = {row['audio_sha256'] for row in rows}
        retained = np.asarray([row['audio_sha256'] not in hashes for row in source_rows])
        training_rows = [row for row, keep in zip(source_rows, retained) if keep]
        groups = validation_groups(training_rows)
        if len(training_rows) < 30 or len(set(groups)) < 3:
            raise ValueError('Insufficient disjoint source data: ' + source)
        x, y = np.asarray(source_vectors)[retained], y[retained]
        alpha = choose_alpha(x, y, groups, [0, 1])
        fitted = fit_path(x, y, [alpha])[0]
        source_mean = y.mean(0)
        save(WORK / 'models' / (source + '.json'), {'format': 'external-new-corpus-source-ridge-1',
            'source': source, 'target_names': TARGETS, 'model': serializable(fitted),
            'training_ids': [row['id'] for row in training_rows], 'source_provenance': source_provenance,
            'target_feature_provenance': provenance, 'source_mean': source_mean.tolist()})
        for subset in sorted({row['subset'] for row in rows}):
            for slice_name in ('all_audio', 'no_known_corpus_overlap'):
                target = [row for row in rows if row['subset'] == subset and
                          (slice_name == 'all_audio' or row['id'] not in overlap['known_corpus_overlaps'])]
                case = source + '__to__' + subset + '__' + slice_name
                item = {'state': 'insufficient_data', 'source': source, 'subset': subset, 'slice': slice_name,
                    'source_rows': len(training_rows), 'source_groups': len(set(groups)),
                    'target_rows': len(target), 'source_alpha': alpha, 'source_provenance': source_provenance}
                report['directions'][case] = item
                target_groups = validation_groups(target)
                item['target_groups'] = len(set(target_groups))
                if len(target) < 10 or len(set(target_groups)) < 3:
                    continue
                observed = np.asarray([row['targets'] for row in target])
                baseline = np.broadcast_to(source_mean, observed.shape).copy()
                predictions = {'source_mean': baseline, 'neutral_midpoint': np.full_like(observed, .5),
                    'ridge_mean_embedding': predict(fitted, np.asarray([vectors[row['id']].mean(0) for row in target])),
                    'ridge_mean_chunk_prediction': np.asarray([predict(fitted, vectors[row['id']]).mean(0) for row in target])}
                item.update(state='complete', target_groups=len(set(target_groups)), methods={})
                for method, values in predictions.items():
                    improvement = bootstrap_improvement(observed, baseline, values, target_groups, [0, 1])
                    improvement['method'] = 'Paired target-group bootstrap of fixed source-selected predictions; conditional on this source fit and pilot selection.'
                    item['methods'][method] = {**metrics(observed, values, TARGETS), 'versus_source_mean': improvement}
                prediction_path = WORK / 'predictions' / (case + '.json')
                save(prediction_path, {'target_ids': [row['id'] for row in target], 'target_names': TARGETS,
                    'observed': observed.tolist(), 'predictions': {name: value.tolist() for name, value in predictions.items()}})
                item['predictions_sha256'] = digest(prediction_path)
                save(WORK / 'transfer-report.json', report)
                print(json.dumps({'new_corpus_transfer': case, 'target_rows': len(target),
                    'mae': {name: result['mae'] for name, result in item['methods'].items()}}), flush=True)
    report['state'] = 'complete'
    save(WORK / 'transfer-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'audit', 'extract', 'transfer', 'repair'])
    args = parser.parse_args()
    if args.action == 'plan':
        plan()
    elif args.action == 'audit':
        audit()
    else:
        lifetime = None
        with threadpool_limits(limits=1):
            if args.action == 'repair':
                repair_intervals(lifetime)
            else:
                extract(lifetime) if args.action == 'extract' else transfer()


if __name__ == '__main__':
    main()
