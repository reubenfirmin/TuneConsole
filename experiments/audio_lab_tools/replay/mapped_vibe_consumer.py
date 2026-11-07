"""Bounded real-library consumer check of the six frozen external scorers.

Production modules run in a separate process against the existing immutable
runtime. Only a new private database copy is writable. No local label scoring.
"""
from collections import Counter
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

import numpy as np

from ..support.external_experiment import LAB, digest, save

BASE = LAB / 'local/external-20261028/checkpoint-1'
CAMPAIGN = BASE / 'library-replay/paired-slices-2026-10-29T150354.951331_0000'
RUNTIME = BASE / 'library-replay/runtime-20261029T125325.116045Z'


def inputs():
    plan = json.loads((CAMPAIGN / 'plan.json').read_text())
    checkpoint = json.loads((BASE / 'reserved-cohort/plan.json').read_text())['candidates']['checkpoint']
    return {'database': str(CAMPAIGN / 'source.db'),
            'database_sha256': plan['frozen_database_sha256'],
            'payload_manifest_sha256': plan['payload_manifest_sha256'],
            'runtime': str(RUNTIME), 'runtime_manifest_sha256': digest(RUNTIME / 'manifest.json'),
            'library_manifest_sha256': digest(BASE / 'inputs/manifest.json'),
            'checkpoint': {key: checkpoint[key] for key in ('path', 'sha256')},
            'publication_limit_s': 600, 'warm_repetitions': 0,
            'scope': 'three compatible excerpts averaged as features; real Mix/Radio publication through an experiment adapter; no product loader or human judgment acceptance'}


def consumer_stage(directory):
    output = directory / 'consumer-worker.log'
    started = time.perf_counter()
    with output.open('w') as log:
        try:
            process = subprocess.run([sys.executable, '-B', '-m', __name__, str(directory)],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=1200, check=False)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 'outer_1200_second_timeout'
    path = directory / 'consumer.report.json'
    if not path.exists():
        save(path, {'state': 'failed', 'returncode': code,
                    'error': 'Consumer subprocess did not produce a final report; see retained log.'})
    report = json.loads(path.read_text())
    report.update(subprocess_returncode=code, subprocess_wall_s=time.perf_counter() - started)
    if code != 0:
        report['state'] = 'failed'
    save(path, report)
    return [path, output, *sorted((directory / 'consumer-output').glob('*.json')),
            *sorted(directory.glob('consumer-shared-intensity.json'))]


def worker(directory):
    from ..studies.mapped_vibe_inference import score
    from ..studies.mapped_vibe_rules import VIBES
    from ..studies.vibe_specialist_round import inside, load
    directory = inside(directory)
    plan = load(directory / 'plan.json')
    pinned = plan['consumer']
    if inputs() != pinned or digest(pinned['database']) != pinned['database_sha256']:
        raise ValueError('Frozen consumer inputs changed')
    if not (directory / 'six-scorers.json').exists():
        raise ValueError('Freeze the six-scorer export first')
    sys.path[:0] = [str(RUNTIME / 'src'), str(RUNTIME)]
    os.environ['TUNECONSOLE_REPLAY_RUNTIME'] = str(RUNTIME)
    from pytest import MonkeyPatch
    from threadpoolctl import threadpool_limits
    from . import library_replay, replay_runtime, replay_storage
    from .external_propagation import current_inputs
    from yt_playlist.rec import audio_vibe_model, musical_character
    from yt_playlist.core import policy_documents, recommendation_outputs
    from .consumer_replay import evaluate
    before = replay_runtime.runtime_hashes()
    replay_storage.verify(CAMPAIGN, pinned['payload_manifest_sha256'])
    bundle = load(directory / 'six-scorers.json')
    library, samples = library_replay.frozen_inputs()
    # Labels and previously stored predictions do not supply outcomes or votes.
    library = {key: value for key, value in library.items() if key not in ('labels', 'predictions', 'prediction_inputs', 'prediction_artists')}
    acoustic, excluded = library_replay.acoustic_inputs(library, samples, bundle['embedding_signature'])
    sample_by_id = {row['id']: sample for values in samples.values() for row, sample in values}
    attributes = []
    for row in acoustic:
        selected = [sample_by_id[key]['audio_attributes']['values'] for key in row['sample_ids']]
        attributes.append([np.mean([v[name] for v in selected]) for name in bundle['attribute_names']])
    data = {'embedding': np.asarray([row['embedding'] for row in acoustic]),
            'attributes': np.asarray(attributes, dtype=float)}
    with threadpool_limits(limits=1):
        scores = np.column_stack([score(bundle['experts'][v], data) for v in VIBES])
    metadata, observations = current_inputs(library, {'provider_measurement', 'audio_measurement'})
    replaced = []
    for row, values in zip(acoustic, scores, strict=True):
        rid = row['recording_id']
        if rid not in metadata:
            continue
        observations[rid] = {'source': 'audio_measurement', 'fits': {
            vibe: {'score': float(value), 'support': 1.} for vibe, value in zip(VIBES, values, strict=True)}}
        replaced.append(rid)
    adapter, calls = library_replay.consumer_adapter(musical_character.load, library, metadata, observations)
    report = {'state': 'running', 'bundle_sha256': digest(directory / 'six-scorers.json'),
              'inputs': pinned, 'recomputed_recordings': replaced, 'excluded_incomplete_recordings': excluded,
              'original_sources_retained': dict(Counter(row['source'] for row in observations.values())),
              'propagated_predictions_as_votes': 0, 'local_labels_used': False,
              'shared_intensity': 'production chooses max(energize,release), preserving slow forceful tracks',
              'limits': ['Experiment adapter, no product loader acceptance or human playlist ratings.',
                         'Existing prepared state is retained; earlier fresh-start evidence remains separate.',
                         '600-second publication diagnostic; does not establish the default 180-second bound.']}
    save(directory / 'consumer.report.json', report)
    output = directory / 'consumer-output'
    if output.exists():
        raise ValueError('Retain an earlier consumer attempt; do not silently overwrite it')
    checkpoint = Path(pinned['checkpoint']['path'])
    if digest(checkpoint) != pinned['checkpoint']['sha256']:
        raise ValueError('Frozen legacy consumer artifact changed')
    payload_calls = {'outside_boundary_reads': 0}
    try:
        with tempfile.TemporaryDirectory(prefix='consumer-private-', dir=directory) as temporary, MonkeyPatch.context() as patch:
            random.seed(136)
            np.random.seed(136)
            patch.setenv('TUNECONSOLE_BENCH_DB', pinned['database'])
            patch.setenv('TUNECONSOLE_MUSICAL_REPORT_DIR', str(output))
            patch.setenv('TUNECONSOLE_BENCH_NOW', '1793260800')
            patch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(checkpoint))
            patch.setattr(musical_character, 'load', adapter)
            for module, name in ((policy_documents, 'read'), (recommendation_outputs, '_read')):
                original = getattr(module, name)
                def guarded(filename, *args, _original=original, **kwargs):
                    path = Path(filename).resolve()
                    if not (path.is_relative_to(directory) or path.is_relative_to(CAMPAIGN / 'payloads')):
                        payload_calls['outside_boundary_reads'] += 1
                        raise ValueError('Unarchived consumer payload read: ' + str(filename))
                    return _original(filename, *args, **kwargs)
                patch.setattr(module, name, guarded)
            with threadpool_limits(limits=1):
                evaluate(Path(temporary), patch, publication_seconds=pinned['publication_limit_s'],
                         diagnostics={'profile_adapter': calls}, warm_samples=pinned['warm_repetitions'])
        if not calls['adapted_reads']:
            raise ValueError('Consumers did not read adapted external scorer profiles')
        # Verify the production shared-intensity input on each actual captured
        # track's learned fits, including cases where force exceeds activation.
        intensity = []
        for row, values in zip(acoustic, scores, strict=True):
            item = {'musical': {'vibes': {'fits': {v: {'score': float(p), 'source': 'audio_measurement', 'support': 1.}
                          for v, p in zip(VIBES, values, strict=True)}}}}
            actual = musical_character.intensity(item)
            expected = max(values[VIBES.index('energize')], values[VIBES.index('release')])
            if abs(actual - expected) > 1e-12:
                raise ValueError('Shared intensity differs from momentum/force maximum')
            intensity.append({'recording_id': row['recording_id'], 'intensity': actual})
        save(directory / 'consumer-shared-intensity.json', intensity)
        report.update(state='complete', consumer_calls=calls, payload_reads=payload_calls,
                      outputs=load(output / 'report.json'), shared_intensity_rows=len(intensity))
    except BaseException as error:
        report.update(state='failed', error=repr(error), consumer_calls=calls, payload_reads=payload_calls)
        raise
    finally:
        report['runtime_unchanged'] = before == replay_runtime.runtime_hashes()
        report['source_database_unchanged'] = digest(pinned['database']) == pinned['database_sha256']
        replay_storage.verify(CAMPAIGN, pinned['payload_manifest_sha256'])
        if not report['runtime_unchanged'] or not report['source_database_unchanged']:
            report['state'] = 'failed'
        save(directory / 'consumer.report.json', report)


if __name__ == '__main__':
    worker(Path(sys.argv[1]))
