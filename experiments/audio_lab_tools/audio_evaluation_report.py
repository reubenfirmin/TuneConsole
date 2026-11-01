"""Summarize checksummed #424 observations without launching inference."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil

import numpy as np

from .audio_evaluation import BUDGETS, CODE, DEFAULT, load, validate, verify
from .external_experiment import LAB, ROOT, digest, save


def extent(values):
    values = list(values)
    if not values:
        return None
    return {'min': float(min(values)), 'median': float(np.median(values)), 'max': float(max(values))}


def shared_values(model, embedding):
    from yt_playlist.rec import audio_vibe_external as external
    native = external._predict(model['native'], embedding)
    values = np.maximum(external._predict(model['calibration'], native), 1e-8)
    return values / values.sum()


def summarize(directory):
    plan = validate(directory)
    verify(directory)
    archive = directory / 'evaluation-source'
    code = (*CODE, 'experiments/audio_lab_tools/audio_evaluation_report.py',
            'src/yt_playlist/rec/audio_vibe_external.py')
    for name in code:
        target = archive / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(ROOT / name, target)
        if digest(target) != digest(ROOT / name):
            raise ValueError('Archived evaluation source changed: ' + name)
    save(archive / 'manifest.json', {'plan_sha256': digest(directory / 'plan.json'),
        'files': {name: digest(archive / name) for name in code},
        'reporter_sha256': digest(__file__)})
    groups = defaultdict(list)
    for path in sorted((directory / 'comparison').glob('*/report.json')):
        groups[load(path)['case']['subset']].append(load(path))
    result = {'plan': str(directory / 'plan.json'), 'cohorts': {},
        'references': [{k: case.get(k) for k in ('index', 'id', 'artist', 'title', 'license_url',
            'audio_sha256', 'start', 'annotation_unit', 'subset')} for case in plan['cases']],
        'runtime': {'chromium': sorted({r['chromium'] for rows in groups.values() for r in rows}),
                    'hardware': plan['hardware']}, 'captures': []}
    for name, rows in groups.items():
        result['cohorts'][name] = {
            'recordings': len({r['case']['id'] for r in rows}), 'excerpts': len(rows),
            'matched_pcm_maximum_delta': {key: max(r['matched_pcm_native_deltas'][key] for r in rows)
                                          for key in ('embedding', 'tags', 'moods')},
            'worker_seconds': extent(r['resource']['worker_wall_seconds'] for r in rows),
            'cpu_seconds': extent(r['resource']['browser_cpu_seconds'] for r in rows),
            'peak_pss_mib': extent(r['resource']['peak_browser_pss_bytes'] / 2**20 for r in rows),
            'independent_decode': {policy: {
                'pcm_rms': extent(r['independent_decoder_sensitivity'][policy]['rms_delta'] for r in rows),
                'embedding_max_delta': extent(r['independent_decoder_sensitivity'][policy]['embedding_maximum_delta'] for r in rows),
                'cruise_absolute_score_delta': extent(r['independent_decoder_sensitivity'][policy]['cruise_absolute_score_delta'] for r in rows)}
                for policy in ('default', 'arithmetic')}}
        model = load(ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json')
        sensitivity = {}
        for policy in ('default', 'arithmetic'):
            deltas, choices = [], 0
            for row in rows:
                case = directory / 'comparison' / f"{row['case']['index']:03d}"
                baseline = shared_values(model, load(case / 'browser.native.json')['embedding'])
                alternative = shared_values(model, load(case / (policy + '.native.json'))['embedding'])
                deltas.append(float(np.max(np.abs(baseline - alternative))))
                choices += int(baseline.argmax() != alternative.argmax())
            sensitivity[policy] = {'maximum_shared_score_delta': extent(deltas),
                'shared_top_choice_changes': choices}
        result['cohorts'][name]['shared_decoder_sensitivity'] = sensitivity
    for path in sorted((directory / 'captures').glob('*/report.json')):
        row = load(path)
        capture = {key: row[key] for key in ('case', 'profile', 'wall_seconds',
            'sampled_browser_cpu_seconds', 'peak_browser_pss_bytes', 'playback', 'numerical_pass',
            'resource_pass', 'playback_pass', 'browser_root_cpu_affinity')}
        capture['worker_wall_seconds'] = row['result']['wall_ms'] / 1000
        capture['worker_budget_pass'] = capture['worker_wall_seconds'] <= BUDGETS['worker_wall_seconds']
        result['captures'].append(capture)
    arcs = defaultdict(list)
    for rows in groups.values():
        for row in rows:
            if row['case']['excerpt_count'] == 3:
                arcs[row['case']['id']].append({'start': row['case']['start'],
                    'cruise': row['browser_cruise']['score'], 'annotation_unit': row['case']['annotation_unit']})
    result['ordered_excerpt_observations'] = dict(arcs)
    save(directory / 'details.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(LAB / 'local'):
        parser.error('Use an existing evaluation directory under experiments/audio_lab/local')
    summarize(directory)


if __name__ == '__main__':
    main()
