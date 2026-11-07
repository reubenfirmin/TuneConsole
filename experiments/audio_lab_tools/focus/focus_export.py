"""Package the user-selected #615 direct Focus heads without refitting."""
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
from yt_playlist.rec import audio_vibe_focus as runtime
from ..support.external_experiment import LAB, digest, save, stamp
from .focus_experiment import arrays, check_receipt, inside, load
from .focus_mapping import exported_predict
from ..relax.relax_experiment import validate

ORIGINAL = LAB / 'local/focus-615-20261030/round-1'
LOCAL = LAB / 'local/focus-615-20261030/local-1'
SOURCE = ORIGINAL / 'exports/direct.json'
SOURCE_SHA256 = '39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098'
PLAN_SHA256 = '5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486'


def captures():
    check_receipt(LOCAL, 'receipt.json', expected_plan=False)
    return [(row, [sample_files.read(LOCAL / name) for name in row['sample_files']])
            for row in load(LOCAL / 'labels.json')['rows']]


def export(directory, install=False):
    if directory.exists():
        raise ValueError('Choose a new export directory; preserve prior evidence')
    if digest(SOURCE) != SOURCE_SHA256 or digest(ORIGINAL / 'plan.json') != PLAN_SHA256:
        raise ValueError('Selected #615 candidate changed')
    validate(ORIGINAL)
    check_receipt(ORIGINAL, 'full.receipt.json')
    check_receipt(ORIGINAL, 'evaluation.receipt.json')
    original = load(SOURCE)
    if (original['format'] != 'focus-mapping-candidate-1' or original['procedure'] != 'direct'
            or original['target_names'] != runtime.TARGETS
            or original['aggregation'] != runtime.AGGREGATION
            or original['training_aggregation'] != runtime.TRAINING_AGGREGATION
            or original['source_plan_sha256'] != PLAN_SHA256):
        raise ValueError('Selected candidate contract changed')
    data_path = ORIGINAL / 'inputs/cal500-raw-activity.npz'
    data = arrays(data_path)
    notice = runtime.DEFAULT_PATH.with_suffix('.NOTICE.md')
    header = {**original, 'format': runtime.FORMAT, 'taxonomy_version': TAXONOMY_VERSION,
              'feature_version': audio_embedding.VERSION, 'feature_names': list(audio_embedding.FEATURES),
              'vibe': runtime.VIBE, 'weights': [.5, .5], 'training_rows': len(data['y']),
              'score_semantics': 'independent_uncalibrated_suitability',
              'source_model_sha256': SOURCE_SHA256,
              'source_terms': 'CAL500: copyright UC San Diego 2007; retain original educational/research permission and restrictions. See focus-vibes.NOTICE.md.'}
    inputs = {str(p): digest(p) for p in (SOURCE, ORIGINAL / 'plan.json', data_path,
        ORIGINAL / 'full.receipt.json', ORIGINAL / 'evaluation.receipt.json', LOCAL / 'receipt.json',
        Path(__file__), Path(runtime.__file__), Path(runtime.bounded.__file__), notice)}
    directory.mkdir(parents=True)
    for name, path in (('export.py', Path(__file__)), ('runtime.py', Path(runtime.__file__)),
                        ('bounded.py', Path(runtime.bounded.__file__)), ('focus-vibes.NOTICE.md', notice)):
        shutil.copyfile(path, directory / name)
    save(directory / 'plan.json', {'created_at': stamp(), 'ticket': 641, 'inputs': inputs, 'fit': False,
        'decision': 'User authorized the exact #615 direct candidate after reviewing #637/#638; no new model selection.'})
    path = directory / 'focus-vibes.json'
    path.write_text(json.dumps(header, separators=(',', ':'), allow_nan=False) + '\n')
    start = time.perf_counter()
    model = runtime.load(path)
    load_seconds = time.perf_counter() - start
    errors, timings = [], []

    def compare(values):
        x = np.asarray(values)
        expected = exported_predict(original, x[None, :, :], np.array([len(x)]), independent=True)[0]
        start = time.perf_counter()
        actual = runtime.score(model, values)
        timings.append(time.perf_counter() - start)
        errors.extend(abs(actual['native'][k] - v) for k, v in zip(runtime.TARGETS, expected, strict=True))
        errors.append(abs(actual['score'] - float(expected.mean())))

    external = {}
    for field in ('chunks', 'browser'):
        for values, count in zip(data[field], data['counts'], strict=True):
            compare(values[:count].tolist())
        external[field] = len(data['y'])
    local, partial, cases = captures(), 0, 0
    for _row, samples in local:
        partial += len(samples) < 3
        if any(audio_embedding.signature(s) != header['embedding_signature'] for s in samples):
            raise ValueError('Frozen capture provenance differs')
        for count in sorted({1, min(3, len(samples)), len(samples)}):
            compare([s['embedding']['values'] for s in samples[:count]])
            cases += 1
    if max(errors) > 1e-12 or any(digest(p) != h for p, h in inputs.items()):
        raise ValueError('Replay failed or inputs changed')
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if path.stat().st_size > runtime.MAX_BYTES or max(timings) > .1 or peak > 1024 ** 3:
        raise ValueError('Resource bound exceeded')
    report = {'created_at': stamp(), 'artifact_sha256': digest(path), 'artifact_bytes': path.stat().st_size,
        'external_cases': external, 'maximum_native_or_blend_error': max(errors),
        'real_capture_replay': {'recordings': len(local), 'partial_recordings': partial, 'cases': cases, 'labels_scored': False},
        'load_seconds': load_seconds, 'warm_seconds': {'median': float(np.median(timings)), 'maximum': max(timings)},
        'peak_rss_bytes': peak, 'timing_scope': 'Cached-feature inference only; excludes audio encoding and application startup.'}
    save(directory / 'report.json', report)
    save(directory / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
        'outputs': {p.name: digest(p) for p in directory.iterdir() if p.name != 'receipt.json'}})
    if install:
        shutil.copyfile(path, runtime.DEFAULT_PATH)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        export(inside(args.directory), args.install)


if __name__ == '__main__':
    main()
