"""Export and replay the exact #577 aggression candidate, without fitting."""
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
from yt_playlist.rec import audio_vibe_release as runtime
from ..support.external_experiment import LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, head_predictions, inside, load
from ..studies.individual_protocol import metrics

ORIGINAL = LAB / 'local/individual-20261030/remaining-1'
LOCAL = LAB / 'local/release-614-20261030/local-1'
SOURCE = ORIGINAL / 'models/jamendo-human-4shards.embedding.json'
SOURCE_SHA256 = '4591b47b47775fe095a5260feda4e9819abd61b9b663834595fa6c9bffebe415'
PLAN_SHA256 = 'c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae'


def captures():
    check_receipt(LOCAL, 'receipt.json', expected_plan=False)
    return [(row, [sample_files.read(LOCAL / name) for name in row['sample_files']])
            for row in load(LOCAL / 'labels.json')['rows']]


def export(directory, install=False):
    if directory.exists():
        raise ValueError('Choose a new export directory; preserve previous evidence')
    if digest(SOURCE) != SOURCE_SHA256 or digest(ORIGINAL / 'plan.json') != PLAN_SHA256:
        raise ValueError('The selected #577 candidate changed')
    check_receipt(ORIGINAL, 'fit-jamendo-human-4shards.receipt.json')
    check_receipt(ORIGINAL, 'evaluation.receipt.json')
    original, source_plan = load(SOURCE), load(ORIGINAL / 'plan.json')
    head = original['heads'][original['target_names'].index('aggressive')]
    artifact = {'format': runtime.FORMAT, 'taxonomy_version': TAXONOMY_VERSION,
                'feature_version': audio_embedding.VERSION, 'feature_names': list(audio_embedding.FEATURES),
                'embedding_signature': original['embedding_signature'],
                'vibe': 'release', 'target': 'aggressive', 'aggregation': 'mean_clipped_native',
                'score_semantics': 'independent_uncalibrated_suitability', 'model': head,
                'training_rows': original['training_rows'], 'source_model_sha256': SOURCE_SHA256,
                'source_plan_sha256': PLAN_SHA256, 'source': original['source'],
                'source_terms': source_plan['jamendo_new_artifacts']}
    paths = [SOURCE, ORIGINAL / 'plan.json', ORIGINAL / 'evaluation.receipt.json',
             ORIGINAL / 'primary.predictions.npz', ORIGINAL / 'inputs/soundtracks-all.npz',
             ORIGINAL / 'inputs/soundtracks-all.json', LOCAL / 'receipt.json',
             Path(__file__), Path(runtime.__file__), Path(runtime.bounded.__file__)]
    inputs = {str(p): digest(p) for p in paths}
    directory.mkdir(parents=True)
    for name, source in (('export.py', Path(__file__)), ('runtime.py', Path(runtime.__file__)),
                         ('bounded.py', Path(runtime.bounded.__file__))):
        shutil.copyfile(source, directory / name)
    save(directory / 'plan.json', {'created_at': stamp(), 'inputs': inputs, 'fit': False,
         'decision': 'User selected the exact #577 aggression candidate after #614 mapping comparison.',
         'limits': 'Previously inspected anger transfer, not direct Release suitability confirmation.'})
    path = directory / 'release-vibes.json'
    path.write_text(json.dumps(artifact, separators=(',', ':'), allow_nan=False) + '\n')
    before = time.perf_counter()
    model = runtime.load(path)
    load_seconds = time.perf_counter() - before
    data = arrays(ORIGINAL / 'inputs/soundtracks-all.npz')
    reference = arrays(ORIGINAL / 'primary.predictions.npz')
    prefix = 'soundtracks-all__release__'
    y = reference[prefix + 'observed']
    reports, timings = {}, []
    for field, suffix in (('embedding', 'embedding'), ('browser', 'browser')):
        predicted = []
        for row in data[field]:
            before = time.perf_counter()
            predicted.append(runtime.score(model, [row.tolist()])['score'])
            timings.append(time.perf_counter() - before)
        error = float(np.max(np.abs(predicted - reference[prefix + 'jamendo-human-4shards_' + suffix])))
        if error > 1e-12:
            raise ValueError('Packaged inference differs from the selected candidate')
        reports[field] = {'rows': len(y), 'maximum_error': error,
                         'metrics': metrics(y, np.asarray(predicted), data['outer'])}
    cases, partial, maximum_error = 0, 0, 0.
    local = captures()
    for _row, samples in local:
        partial += len(samples) < 3
        if any(audio_embedding.signature(s) != artifact['embedding_signature'] for s in samples):
            raise ValueError('Incompatible frozen capture')
        for count in sorted({1, min(3, len(samples)), len(samples)}):
            values = [s['embedding']['values'] for s in samples[:count]]
            expected = float(head_predictions([head], np.asarray(values), True).mean())
            before = time.perf_counter()
            actual = runtime.score(model, values)['score']
            timings.append(time.perf_counter() - before)
            maximum_error = max(maximum_error, abs(actual - expected))
            cases += 1
    if maximum_error > 1e-12 or any(digest(p) != h for p, h in inputs.items()):
        raise ValueError('Capture replay failed or inputs changed')
    report = {'created_at': stamp(), 'artifact_sha256': digest(path), 'artifact_bytes': path.stat().st_size,
              'training_rows': artifact['training_rows'], 'historical_and_browser': reports,
              'real_capture_replay': {'recordings': len(local), 'partial_recordings': partial,
                                     'cases': cases, 'maximum_error': maximum_error, 'labels_scored': False},
              'load_seconds': load_seconds, 'warm_seconds': {'median': float(np.median(timings)), 'maximum': max(timings)},
              'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              'timing_scope': 'Cached-feature inference only; excludes encoder, extraction and application startup.',
              'source_terms': artifact['source_terms']}
    save(directory / 'report.json', report)
    save(directory / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
         'outputs': {p.name: digest(p) for p in directory.iterdir() if p.name != 'receipt.json'}})
    if install:
        shutil.copyfile(path, runtime.DEFAULT_PATH)
    print(json.dumps({k: v for k, v in report.items() if k != 'historical_and_browser'}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--install', action='store_true', help='Copy the replay-verified artifact into the product package')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        export(inside(args.directory), args.install)


if __name__ == '__main__':
    main()
