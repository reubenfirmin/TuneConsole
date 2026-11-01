"""Export #616's user-selected no-joy consensus without fitting new models."""
import argparse
import json
from pathlib import Path
import resource
import shutil
import time

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.core import audio_embedding, sample_files
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_energize as runtime
from .external_experiment import LAB, digest, save, stamp
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load
from .energize_consensus import predict
from .energize_experiment import validate

ORIGINAL = LAB / 'local/energize-616-20261030/round-1'
LOCAL = LAB / 'local/energize-616-20261030/local-1'
SOURCE = ORIGINAL / 'models/full.json'
SOURCE_SHA256 = '0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e'
PLAN_SHA256 = '0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42'


def captures():
    check_receipt(LOCAL, 'receipt.json', expected_plan=False)
    return [(row, [sample_files.read(LOCAL / name) for name in row['sample_files']])
            for row in load(LOCAL / 'labels.json')['rows']]


def artifact(original, plan):
    if (original['format'] != 'energize-consensus-1'
            or original['fixed_weights'] != [.5, .25, .125, .125]
            or original['aggregation'] != 'mean_clipped_native_then_fixed_or_activity_map'
            or original['training_aggregation'] != runtime.TRAINING_AGGREGATION):
        raise ValueError('The selected frozen source model changed')
    experts = {}
    for source, targets in runtime.TARGETS.items():
        expert = original['experts'][source]
        head = expert['heads'][expert['target_names'].index(targets[0])]
        if head['kind'] != 'ridge' or len(head['intercept']) != 1:
            raise ValueError('Unexpected frozen native head')
        experts[source] = {'target_names': targets, 'model': head,
                           'training_rows': len(arrays(ORIGINAL / f'inputs/{source}.npz')['ids'])}
    return {'format': runtime.FORMAT, 'taxonomy_version': TAXONOMY_VERSION,
            'feature_version': audio_embedding.VERSION, 'feature_names': list(audio_embedding.FEATURES),
            'embedding_signature': plan['embedding_signature'], 'vibe': 'energize',
            'aggregation': runtime.AGGREGATION, 'training_aggregation': runtime.TRAINING_AGGREGATION,
            'procedure': runtime.PROCEDURE, 'weights': runtime.WEIGHTS, 'experts': experts,
            'score_semantics': 'independent_uncalibrated_suitability',
            'source_model_sha256': SOURCE_SHA256, 'source_plan_sha256': PLAN_SHA256,
            'source_terms': plan['terms']}


def export(directory, install=False):
    if directory.exists():
        raise ValueError('Choose a new export directory; preserve previous evidence')
    if digest(SOURCE) != SOURCE_SHA256 or digest(ORIGINAL / 'plan.json') != PLAN_SHA256:
        raise ValueError('The selected #616 candidate changed')
    plan = validate(ORIGINAL)
    check_receipt(ORIGINAL, 'full.receipt.json')
    check_receipt(ORIGINAL, 'evaluation.receipt.json')
    original = load(SOURCE)
    header = artifact(original, plan)
    paths = [SOURCE, ORIGINAL / 'plan.json', ORIGINAL / 'full.receipt.json',
             ORIGINAL / 'evaluation.receipt.json', LOCAL / 'receipt.json',
             Path(__file__), Path(runtime.__file__), Path(runtime.bounded.__file__)]
    paths += [ORIGINAL / f'inputs/{source}.npz' for source in original['experts']]
    inputs = {str(p): digest(p) for p in paths}
    directory.mkdir(parents=True)
    for name, source in (('export.py', Path(__file__)), ('runtime.py', Path(runtime.__file__)),
                         ('bounded.py', Path(runtime.bounded.__file__))):
        shutil.copyfile(source, directory / name)
    save(directory / 'plan.json', {'created_at': stamp(), 'inputs': inputs, 'fit': False,
         'decision': 'User selected consensus_no_joy for broader native-label coverage (#631), accepting reported losses.',
         'limits': 'Coefficient replay does not change the frozen research retain recommendation.'})
    path = directory / 'energize-vibes.json'
    path.write_text(json.dumps(header, separators=(',', ':'), allow_nan=False) + '\n')
    before = time.perf_counter()
    model = runtime.load(path)
    load_seconds = time.perf_counter() - before
    errors, timings, cases = [], [], 0

    def compare(values):
        nonlocal cases
        x = np.asarray(values)
        expected = float(predict(original, x[None, :, :], np.array([len(x)]), independent=True)[runtime.PROCEDURE][0])
        before = time.perf_counter()
        actual = runtime.score(model, values)
        timings.append(time.perf_counter() - before)
        errors.append(abs(actual['score'] - expected))
        for source, targets in runtime.TARGETS.items():
            expert = original['experts'][source]
            head = expert['heads'][expert['target_names'].index(targets[0])]
            native = head_predictions([head], x, independent=True).mean(axis=0)[0]
            errors.append(abs(actual['native'][source][targets[0]] - native))
        cases += 1

    external = {}
    for source in original['experts']:
        data = arrays(ORIGINAL / f'inputs/{source}.npz')
        for field in ('chunks', 'browser'):
            if field not in data:
                continue
            start = cases
            for values, count in zip(data[field], data['counts'], strict=True):
                compare(values[:count].tolist())
            external[source + '/' + field] = cases - start
    local = captures()
    start, partial = cases, 0
    for _row, samples in local:
        partial += len(samples) < 3
        if any(audio_embedding.signature(s) != header['embedding_signature'] for s in samples):
            raise ValueError('Incompatible frozen capture')
        for count in sorted({1, min(3, len(samples)), len(samples)}):
            compare([s['embedding']['values'] for s in samples[:count]])
    if max(errors) > 1e-12 or any(digest(p) != h for p, h in inputs.items()):
        raise ValueError('Coefficient replay failed or inputs changed')
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if path.stat().st_size > runtime.MAX_BYTES or max(timings) > .1 or peak > 1024 ** 3:
        raise ValueError('Bounded artifact/inference resource limit exceeded')
    report = {'created_at': stamp(), 'artifact_sha256': digest(path), 'artifact_bytes': path.stat().st_size,
              'external_cases': external, 'maximum_native_or_blend_error': max(errors),
              'real_capture_replay': {'recordings': len(local), 'partial_recordings': partial,
                                     'cases': cases - start, 'labels_scored': False},
              'load_seconds': load_seconds, 'warm_seconds': {'median': float(np.median(timings)), 'maximum': max(timings)},
              'peak_rss_bytes': peak,
              'timing_scope': 'Cached-feature inference only; excludes encoder, extraction and application startup.',
              'source_terms': header['source_terms']}
    save(directory / 'report.json', report)
    save(directory / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
         'outputs': {p.name: digest(p) for p in directory.iterdir() if p.name != 'receipt.json'}})
    if install:
        shutil.copyfile(path, runtime.DEFAULT_PATH)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--install', action='store_true', help='Bundle the replay-verified coefficients')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        export(inside(args.directory), args.install)


if __name__ == '__main__':
    main()
