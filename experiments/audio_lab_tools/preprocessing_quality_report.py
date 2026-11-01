"""Lightweight receipt/cache audit and compact preprocessing-quality results."""
import argparse
import json
from pathlib import Path
import shutil

import numpy as np

from .external_experiment import LAB, digest, save
from .preprocessing_quality import (COHORTS, DEFAULT, ERROR_LIMIT, PILOTS,
    case_path, check, checked_case, input_arrays, load, validate)


def report(directory):
    plan = validate(directory)
    result = {'plan_sha256': digest(directory / 'plan.json'), 'cohorts': {},
              'completed_recordings': 0, 'completed_chunks': 0, 'planned_recordings': len(plan['cases'])}
    for cohort in COHORTS:
        arrays = input_arrays(directory / 'inputs' / (cohort + '.npz'))
        offsets = arrays['offsets'] if cohort in PILOTS else np.arange(len(arrays['y']) + 1)
        rows, chunks, cache_error, sentinels, failed = 0, 0, 0., [], []
        for case in (c for c in plan['cases'] if c['cohort'] == cohort):
            target = case_path(directory, case)
            if not (target / 'receipt.json').exists():
                if (target / 'failure.json').exists():
                    failed.append(load(target / 'failure.json'))
                continue
            row = checked_case(directory, case)
            begin, end = offsets[case['index']:case['index'] + 2]
            current = np.asarray([c['ffmpeg']['embedding'] for c in row['chunks']])
            cache_error = max(cache_error, float(np.max(np.abs(current - arrays['embedding'][begin:end]))))
            sentinels.extend(c['production_sentinel']['maximum_absolute_error']
                             for c in row['chunks'] if 'production_sentinel' in c)
            rows += 1
            chunks += len(row['chunks'])
        result['cohorts'][cohort] = {'recordings': rows, 'chunks': chunks,
            'historical_cache_maximum_error': cache_error, 'historical_cache_pass': cache_error <= ERROR_LIMIT,
            'sentinels': len(sentinels), 'sentinel_maximum_error': max(sentinels, default=None), 'failures': failed}
        result['completed_recordings'] += rows
        result['completed_chunks'] += chunks
    receipt_path = directory / 'evaluation.receipt.json'
    if receipt_path.exists():
        receipt = load(receipt_path)
        if receipt['plan_sha256'] != result['plan_sha256']:
            raise ValueError('Evaluation belongs to another plan')
        check([{'path': str(directory / name), 'sha256': value} for name, value in receipt['outputs'].items()])
        quality = load(directory / 'quality.report.json')
        compact = {}
        for cohort, item in quality['cases'].items():
            compact[cohort] = {'retained': item['retained'], 'vibes': {}}
            for vibe, value in item['vibes'].items():
                compact[cohort]['vibes'][vibe] = {
                    'ndcg': {name: metric['ndcg'] for name, metric in value['metrics'].items()},
                    'mse': {name: metric['mse'] for name, metric in value['metrics'].items()},
                    'paired': value['paired'], 'score_difference': value['score_difference']}
        result['quality'] = compact
    # Keep each reporter revision without rewriting the campaign's frozen source.
    source = Path(__file__)
    archive = directory / 'supplementary-source' / (digest(source) + '.py')
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        shutil.copyfile(source, archive)
    result['reporter_sha256'] = digest(source)
    save(directory / 'progress.report.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(LAB / 'local'):
        parser.error('Use an experiment-local directory')
    report(directory)


if __name__ == '__main__':
    main()
