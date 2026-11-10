"""Export the user-selected #618 fixed consensus without fitting new models."""
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
from yt_playlist.rec import audio_vibe_uplift as runtime
from ..support.external_experiment import LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, head_predictions, inside, load
from .uplift_consensus import predict
from .uplift_experiment import validate

ORIGINAL = LAB / 'local/uplift-618-20261030/round-1'
LOCAL = LAB / 'local/uplift-618-20261030/local-1'
SOURCE = ORIGINAL / 'models/full.json'
SOURCE_SHA256 = 'a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737'
PLAN_SHA256 = '2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66'


def captures():
    check_receipt(LOCAL, 'receipt.json', expected_plan=False)
    return [(row, [sample_files.read(LOCAL / name) for name in row['sample_files']])
            for row in load(LOCAL / 'labels.json')['rows']]


def artifact(original, plan):
    if (original['expert_names'] != ['deam', 'pmemo', 'soundtracks-all', 'jamendo-human-4shards',
                                     'emotify-artist', 'manymusic', 'memo2496-pilot']
            or original['fixed_weights'] != [.125, .125, .25, .125, .125, .125, .125]
            or original['aggregation'] != runtime.AGGREGATION
            or original['training_aggregation'] != runtime.TRAINING_AGGREGATION):
        raise ValueError('The selected fixed consensus changed')
    experts = {}
    for source, targets in runtime.TARGETS.items():
        expert = original['experts'][source]
        heads = expert['heads']
        if expert['target_names'] != targets or len(heads) != len(targets):
            raise ValueError('Native target order changed')
        if any(h['kind'] != 'ridge' or any(h[k] != heads[0][k] for k in ('medians', 'mean', 'scale')) for h in heads):
            raise ValueError('Native preprocessing cannot be combined exactly')
        model = {k: heads[0][k] for k in ('kind', 'medians', 'mean', 'scale')}
        model.update(weights=[[h['weights'][j][0] for h in heads] for j in range(audio_embedding.DIMENSIONS)],
                     intercept=[h['intercept'][0] for h in heads], alpha_by_target=[h['alpha'] for h in heads])
        experts[source] = {'target_names': targets, 'model': model,
                           'training_rows': len(arrays(ORIGINAL / f'inputs/{source}.npz')['ids'])}
    return {'format': runtime.FORMAT, 'taxonomy_version': TAXONOMY_VERSION,
            'feature_version': audio_embedding.VERSION, 'feature_names': list(audio_embedding.FEATURES),
            'embedding_signature': plan['embedding_signature'], 'vibe': 'uplift',
            'aggregation': runtime.AGGREGATION, 'training_aggregation': runtime.TRAINING_AGGREGATION,
            'weights': runtime.WEIGHTS, 'experts': experts,
            'score_semantics': 'independent_uncalibrated_suitability',
            'source_model_sha256': SOURCE_SHA256, 'source_plan_sha256': PLAN_SHA256,
            'source_terms': plan['terms']}


def export(directory, install=False):
    if directory.exists():
        raise ValueError('Choose a new export directory; preserve previous evidence')
    if digest(SOURCE) != SOURCE_SHA256 or digest(ORIGINAL / 'plan.json') != PLAN_SHA256:
        raise ValueError('The selected #618 candidate changed')
    plan = validate(ORIGINAL)
    check_receipt(ORIGINAL, 'full.receipt.json')
    check_receipt(ORIGINAL, 'evaluation.receipt.json')
    original = load(SOURCE)
    header = artifact(original, plan)
    paths = [SOURCE, ORIGINAL / 'plan.json', ORIGINAL / 'full.receipt.json',
             ORIGINAL / 'evaluation.receipt.json', LOCAL / 'receipt.json',
             Path(__file__), Path(runtime.__file__), Path(runtime.bounded.__file__)]
    paths += [ORIGINAL / f'inputs/{source}.npz' for source in runtime.TARGETS]
    inputs = {str(p): digest(p) for p in paths}
    directory.mkdir(parents=True)
    for name, source in (('export.py', Path(__file__)), ('runtime.py', Path(runtime.__file__)),
                         ('bounded.py', Path(runtime.bounded.__file__))):
        shutil.copyfile(source, directory / name)
    save(directory / 'plan.json', {'created_at': stamp(), 'inputs': inputs, 'fit': False,
         'decision': 'User selected the fixed #618 blend, accepting the generated-music regression (#630).',
         'limits': 'Coefficient replay is not an independent accuracy evaluation. Frozen research retains its no-go result.'})
    path = directory / 'uplift-vibes.json'
    path.write_text(json.dumps(header, separators=(',', ':'), allow_nan=False) + '\n')
    before = time.perf_counter()
    model = runtime.load(path)
    load_seconds = time.perf_counter() - before
    errors, timings, cases = [], [], 0

    def compare(values):
        nonlocal cases
        x = np.asarray(values)
        expected = float(predict(original, x[None, :, :], np.array([len(x)]), independent=True)['fixed'][0])
        before = time.perf_counter()
        actual = runtime.score(model, values)
        timings.append(time.perf_counter() - before)
        errors.append(abs(actual['score'] - expected))
        for source, expert in original['experts'].items():
            native = head_predictions(expert['heads'], x, independent=True).mean(axis=0)
            errors.extend(abs(actual['native'][source][label] - value)
                          for label, value in zip(expert['target_names'], native, strict=True))
        cases += 1

    external = {}
    for source in runtime.TARGETS:
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
    if path.stat().st_size > runtime.MAX_BYTES or max(timings) > .1:
        raise ValueError('Bounded artifact/inference resource limit exceeded')
    report = {'created_at': stamp(), 'artifact_sha256': digest(path), 'artifact_bytes': path.stat().st_size,
              'external_cases': external, 'maximum_native_or_blend_error': max(errors),
              'real_capture_replay': {'recordings': len(local), 'partial_recordings': partial,
                                     'cases': cases - start, 'labels_scored': False},
              'load_seconds': load_seconds, 'warm_seconds': {'median': float(np.median(timings)), 'maximum': max(timings)},
              'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
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
