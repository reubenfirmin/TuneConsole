"""Adaptive engine and feature comparisons on independently prepared inputs.

./experiments/run.py investigate bench_taste_groups.py -s
Optional --group-snapshot / --group-readonly-db freezes prepared inputs read-only.
Recurring production budget and lineage acceptance remains in tests/bench_taste_groups.py.
"""
import json

from tests.group_benchmark_support import frozen_inputs, report_path
from tests.group_support import recordings
from experiments.investigations import group_eval
from yt_playlist.rec import group_discovery, group_similarity


def test_compare_adaptive_engines_on_independently_prepared_libraries(tmp_path, request):
    cases = {
        'small-album': recordings([('Pop', 8, .25)]),
        'metal-plus-pop': recordings([('Death Metal', 180, .9), ('Thrash Metal', 60, .8), ('Pop', 12, .25)]),
        'eclectic': recordings([('Death Metal', 45, .9), ('House', 45, .7), ('Ambient', 45, .1),
                               ('Jazz', 45, .4), ('Pop', 12, .3)]),
        'same-style-two-sounds': recordings([('House', 45, .1), ('House', 45, .95)]),
    }
    frozen = frozen_inputs(request, tmp_path)
    if frozen is not None:
        cases['frozen-library'] = [group_similarity.recording(key, payload['library'], profile)
            for key, payload, profile in frozen['rows'] if payload['library'] is not None]
    reports = {}
    for name, tracks in cases.items():
        reports[name] = group_eval.compare(tracks, policy=group_discovery.Policy(max_groups=512))
        print(json.dumps({'case': name, 'n': len(tracks), 'engines': {engine: {
            **{key: row.get(key) for key in ('leaves', 'assigned', 'unknown', 'edges', 'named_leaves', 'seconds', 'growth_ari')},
            'retained_small_styles': sum(row['minority_style_retention'].values()),
            'small_styles': len(row['minority_style_retention']), 'largest': max(row['sizes'], default=0)}
            for engine, row in reports[name]['engines'].items()}}), flush=True)
        assert all(row['assigned'] + row['unknown'] == len(tracks) for row in reports[name]['engines'].values())
    if frozen is not None:
        reports['feature-comparison'] = group_eval.feature_comparison(cases['frozen-library'])
        print(json.dumps({'feature-comparison': {name: {key: row.get(key) for key in
            ('leaves', 'assigned', 'unknown', 'seconds', 'deferred')}
            for name, row in reports['feature-comparison'].items()}}), flush=True)
    output = report_path(request, tmp_path, 'group-comparison.json')
    output.write_text(json.dumps(reports, indent=2))
    print('Report:', output)
