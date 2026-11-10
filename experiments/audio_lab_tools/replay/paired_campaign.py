"""Run one fixed consumer candidate per invocation, retaining failed attempts."""
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile

import numpy as np
from pytest import MonkeyPatch

from . import consumer_replay, library_replay, replay_runtime, replay_storage
from ..support.external_experiment import ROOT, digest, save, stamp

CHOICES = (('checkpoint', 'prediction'), ('core_consensus', 'selected_candidate'),
           ('consensus_expanded', 'selected_candidate'), ('expanded_rankings', 'selected_candidate'),
           ('expanded_semantics', 'equal_all_activity_support'))


def code_hashes():
    paths = [__file__, library_replay.__file__, consumer_replay.__file__, replay_runtime.__file__, replay_storage.__file__,
             ROOT / 'experiments/run.py', *library_replay.inference_hashes()]
    return {str(Path(path).relative_to(ROOT)): digest(path) for path in paths}


def verify_code_archive(target, expected):
    directory = target / 'code'
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file()}
    if actual != set(expected) or any(digest(directory / name) != value for name, value in expected.items()):
        raise ValueError('Consumer campaign source archive changed')


def validate_plan(plan, current):
    """Refuse completed-stage reuse after any relevant provenance change."""
    for key in ('schema', 'code_sha256', 'models', 'runtime_code_sha256', 'runtime_snapshot',
                'runtime_snapshot_manifest_sha256', 'input_manifest_sha256',
                'runtime_environment', 'publication_limit_s', 'choices', 'capture_seconds_per_copy', 'execution_mode'):
        if plan.get(key) != current.get(key):
            raise ValueError('Paired campaign provenance changed: ' + key)


def verify_receipts(target, completed):
    for name, result in completed.items():
        output = (target / result['output']).resolve()
        if not output.is_relative_to(target.resolve()):
            raise ValueError('Consumer output escapes its campaign: ' + name)
        receipt = output / 'result.json'
        if (digest(receipt) != result['receipt_sha256'] or json.loads(receipt.read_text()) !=
                {key: value for key, value in result.items() if key != 'receipt_sha256'}):
            raise ValueError('Completed consumer receipt changed: ' + name)
        for relative, expected in result['output_sha256'].items():
            path = (target / relative).resolve()
            if not path.is_relative_to(output) or digest(path) != expected:
                raise ValueError('Completed consumer output changed: ' + name + '/' + relative)
        actual = {str(path.relative_to(target.resolve())) for path in output.iterdir()
                  if path.is_file() and path.name != 'result.json'}
        if actual != set(result['output_sha256']):
            raise ValueError('Completed consumer output file set changed: ' + name)


def seed_digest(seed):
    return hashlib.sha256(json.dumps(seed, sort_keys=True, allow_nan=False).encode()).hexdigest()


def run(database, *, publication_seconds, resume=None, independent_execution=True):
    from tests.bench_svd_background import _backup_library
    from experiments.investigations.bench_musical_character import test_saved_catalog_musical_character_audit
    from yt_playlist.rec import audio_vibe_model, musical_character
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.util import genre_map
    from collections import Counter

    if not replay_runtime.runtime_identity()['runtime_snapshot']:
        raise ValueError('Paired slices require an explicit frozen runtime')
    library, samples = library_replay.frozen_inputs()
    candidates = library_replay.frozen_models()
    checkpoint = json.loads(candidates['checkpoint'].read_text())
    acoustic, excluded = library_replay.acoustic_inputs(library, samples, checkpoint['embedding_signature'])
    metadata, _observations = library_replay.current_inputs(library, {'provider_measurement', 'audio_measurement'})
    counts = Counter(genre_map.family(row['genre']) for row in metadata.values() if row['genre'])
    genres = [genre for genre, _count in counts.most_common(2) if genre]
    current = {'schema': 3, 'code_sha256': code_hashes(),
        'execution_mode': 'independent_experiment',
        'runtime_environment': {'python': sys.version, 'sqlite': sqlite3.sqlite_version,
            'packages': {name: version(name) for name in ('numpy', 'scipy', 'pytest')}},
        'models': {name: {'path': str(candidates[name]), 'sha256': digest(candidates[name]), 'method': method}
                   for name, method in CHOICES},
        'runtime_code_sha256': replay_runtime.runtime_hashes(), **replay_runtime.runtime_identity(),
        'input_manifest_sha256': digest(library_replay.RUN / 'inputs/manifest.json'),
        'publication_limit_s': publication_seconds, 'choices': [list(row) for row in CHOICES],
        'capture_seconds_per_copy': 0}
    if resume is None:
        target = library_replay.WORK / ('paired-slices-' + stamp().replace(':', '').replace('+', '_'))
        target.mkdir(parents=True, mode=0o700)
        for relative, expected in current['code_sha256'].items():
            raw = (ROOT / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('Research source changed while archiving: ' + relative)
            copied = target / 'code' / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(raw)
        verify_code_archive(target, current['code_sha256'])
        frozen_database = target / 'source.db'
        backup = _backup_library(Path(database), frozen_database)
        frozen_database.chmod(0o600)
        payloads = replay_storage.freeze(frozen_database, target)
        plan = {**current, 'created_at': stamp(), 'database': str(Path(database).resolve()),
            'backup': backup, 'frozen_database_sha256': digest(frozen_database),
            'payload_manifest_sha256': digest(target / 'payload-manifest.json'),
            'payload_records': payloads['records'], 'payload_files': len(payloads['files']),
            'payload_bytes': payloads['bytes'],
            'complete_compatible_audio_recordings': len(acoustic), 'excluded_incomplete_audio': excluded,
            'explicit_genre_families': genres,
            'extended_publication_diagnostic': publication_seconds > 180,
            'policy': 'One fixed candidate per invocation. Same SQL, runtime, observations, seeds and filters. No fit or preparation. Failed attempts remain separate.',
            'limitations': ['Research adapter bypasses product artifact loading and live invalidation.',
                'Retained prepared state is not fresh-user acceptance.',
                'Frozen observations predate the SQL snapshot; unknown identities retain original profiles.',
                'This prepared-state experiment does not import or replay native evidence-ledger history.',
                'No independent human judgments of the generated playlists.',
                'Timing includes the process-local research adapter.',
                'Independent execution may overlap other agents; wall-time comparisons do not control host contention.',
                'An extended diagnostic cannot pass the original 180-second publication attempt.']}
        save(target / 'plan.json', plan)
        state = {'methods': {}, 'attempts': [], 'fixed_radio_seed': [], 'fixed_radio_seed_sha256': seed_digest([]),
                 'plan_sha256': digest(target / 'plan.json')}
        save(target / 'progress.json', state)
    else:
        target = Path(resume).resolve()
        if not target.is_relative_to(library_replay.WORK.resolve()) or not target.name.startswith('paired-slices-'):
            raise ValueError('Expected an experiments-only paired-slices campaign')
        plan = json.loads((target / 'plan.json').read_text())
        validate_plan(plan, current)
        verify_code_archive(target, plan['code_sha256'])
        frozen_database = target / 'source.db'
        if digest(frozen_database) != plan['frozen_database_sha256']:
            raise ValueError('Frozen consumer database changed')
        replay_storage.verify(target, plan['payload_manifest_sha256'])
        state = json.loads((target / 'progress.json').read_text())
        if state['plan_sha256'] != digest(target / 'plan.json'):
            raise ValueError('Consumer campaign plan changed')
        if state['fixed_radio_seed_sha256'] != seed_digest(state['fixed_radio_seed']):
            raise ValueError('Common Radio seed changed')
        verify_receipts(target, state['methods'])
    expected_names = [name for name, _method in CHOICES]
    completed_names = list(state['methods'])
    if completed_names != expected_names[:len(completed_names)]:
        raise ValueError('Consumer campaign no longer follows the fixed candidate order')
    pending = [row for row in CHOICES if row[0] not in state['methods']]
    if not pending:
        print(json.dumps({'paired_slices': str(target), 'complete': True, 'already_complete': True}), flush=True)
        return
    name, method = pending[0]
    artifact = json.loads(candidates[name].read_text())
    metadata, observations, audit = library_replay.candidate_observations(library, acoustic, artifact, method)
    adapter, calls = library_replay.consumer_adapter(musical_character.load, library, metadata, observations)
    attempt = {'candidate': name, 'started_at': stamp(), 'status': 'running',
               'host_load_average_at_start': list(os.getloadavg())}
    output = target / name / ('attempt-' + attempt['started_at'].replace(':', '').replace('+', '_'))
    output.parent.mkdir(mode=0o700, exist_ok=True)
    attempt['output'] = str(output.relative_to(target))
    state['attempts'].append(attempt)
    save(target / 'progress.json', state)
    seed_offer = RadioCandidates.seed_offer

    def common_seed(candidate, count=16):
        if count != 1:
            return seed_offer(candidate, count)
        if not state['fixed_radio_seed']:
            if name != 'checkpoint':
                raise ValueError('Missing checkpoint-selected common Radio seed')
            offered = deepcopy(seed_offer(candidate, count))
            if len(offered) != 1:
                raise ValueError('No eligible common Radio seed')
            state.update(fixed_radio_seed=offered, fixed_radio_seed_sha256=seed_digest(offered))
            save(target / 'progress.json', state)
        result = deepcopy(state['fixed_radio_seed'])
        profiles = musical_character.load(candidate.store, [row['key'] for row in result])
        for row in result:
            row['musical'] = profiles.get(row['key'])
        candidate.rng.seed(136)
        return result

    payload_calls = {}
    try:
        with tempfile.TemporaryDirectory(prefix='private-', dir=target) as temporary, MonkeyPatch.context() as patch:
            payload_calls = replay_storage.guard_reads(patch, target)
            random.seed(136)
            np.random.seed(136)
            patch.setenv('TUNECONSOLE_BENCH_DB', str(frozen_database.resolve()))
            patch.setenv('TUNECONSOLE_MUSICAL_REPORT_DIR', str(output.resolve()))
            patch.setenv('TUNECONSOLE_MUSICAL_CAPTURE_SECONDS', '0')
            patch.setenv('TUNECONSOLE_BENCH_NOW', '1793260800')
            patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(candidates['checkpoint']))
            patch.setattr(musical_character, 'load', adapter)
            patch.setattr(RadioCandidates, 'seed_offer', common_seed)
            patch.setattr('experiments.investigations.bench_musical_character.time.time', lambda: 1793260800.)
            if publication_seconds == 180:
                test_saved_catalog_musical_character_audit(Path(temporary), patch)
            else:
                consumer_replay.evaluate(Path(temporary), patch, publication_seconds=publication_seconds,
                    diagnostics={'profile_adapter': calls, 'payload_reads': payload_calls})
            constraints = library_replay.consumer_constraints(Path(temporary) / 'musical-library.db', output,
                genres, common_seed_track=state['fixed_radio_seed'][0])
        if not calls['adapted_reads']:
            raise ValueError('Consumer replay did not read candidate musical profiles: ' + name)
        result = {'observation_audit': audit, 'consumer_calls': calls, 'payload_reads': payload_calls,
            'constraints': constraints,
            'output': str(output.relative_to(target)),
            'report': json.loads((output / 'report.json').read_text()),
            'output_sha256': {str(path.relative_to(target)): digest(path) for path in sorted(output.iterdir())
                              if path.is_file()}}
        save(output / 'result.json', result)
        result['receipt_sha256'] = digest(output / 'result.json')
        state['methods'][name] = result
        attempt['status'] = 'complete'
    except BaseException as error:
        state['methods'].pop(name, None)
        attempt.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=repr(error))
        raise
    finally:
        attempt['finished_at'] = stamp()
        attempt['host_load_average_at_end'] = list(os.getloadavg())
        attempt['consumer_calls'] = dict(calls)
        attempt['payload_reads'] = dict(payload_calls)
        try:
            unchanged = (current['runtime_code_sha256'] == replay_runtime.runtime_hashes()
                         and current['code_sha256'] == code_hashes()
                         and digest(frozen_database) == plan['frozen_database_sha256'])
            replay_storage.verify(target, plan['payload_manifest_sha256'])
        except (OSError, ValueError) as error:
            unchanged = False
            attempt['source_verification_error'] = repr(error)
        if not unchanged:
            state['methods'].pop(name, None)
            attempt.update(status='rejected_source_change')
        save(target / 'progress.json', state)
        complete = unchanged and len(state['methods']) == len(CHOICES)
        comparisons = library_replay.compare_outputs(target, list(state['methods']),
            paths={key: target / result['output'] for key, result in state['methods'].items()}) if state['methods'] else {}
        save(target / 'report.json', {'created_at': stamp(), 'methods': state['methods'], 'complete': complete,
            'attempts': state['attempts'], 'fixed_radio_seed': state['fixed_radio_seed'],
            'output_comparisons': comparisons, 'runtime_code_unchanged_during_run': unchanged,
            'plan_sha256': digest(target / 'plan.json'), 'extended_publication_diagnostic': publication_seconds > 180,
            'execution_mode': current['execution_mode'],
            'publication_limit_s': publication_seconds, 'original_publication_bound_passed': complete and publication_seconds == 180})
    if not unchanged:
        raise ValueError('Consumer campaign source changed during this slice')
    print(json.dumps({'paired_slices': str(target), 'completed': list(state['methods']),
                      'next': next((key for key, _method in CHOICES if key not in state['methods']), None),
                      'complete': complete}), flush=True)
