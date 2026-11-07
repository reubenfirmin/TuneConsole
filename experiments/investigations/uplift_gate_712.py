"""Actual-data independence checks for the arousal-conditioned mixer."""
from collections import defaultdict

import numpy as np

from experiments.audio_lab_tools.uplift import uplift_consensus as m
from experiments.audio_lab_tools.support.external_experiment import LAB
from experiments.audio_lab_tools.focus.focus_experiment import arrays, load
from experiments.audio_lab_tools.relax.relax_experiment import global_groups

OLD = LAB / 'local/uplift-618-20261030/round-1'


def inventory():
    rows, bounds = [], {}
    for source in m.SOURCES:
        data = load(OLD / f'inputs/{source}.json')['rows']
        bounds[source] = slice(len(rows), len(rows) + len(data))
        rows.extend(data)
    for path in sorted((LAB / 'local/per-vibe-20261029/all-sources-1/inputs').glob('*.json')):
        rows.extend(load(path).get('rows', []))
    local = load(LAB / 'local/individual-20261030/local-1/labels.json')['rows']
    boundary = len(rows)
    rows.extend(local)
    return rows, bounds, boundary, global_groups(rows)


def test_stronger_inventory_repairs_prior_partition():
    rows, bounds, boundary, groups = inventory()
    old, assignment, all_groups = [], [], []
    for source, bound in bounds.items():
        data = arrays(OLD / f'inputs/{source}.npz')
        old.extend(data['groups'])
        assignment.extend(data['outer'])
        all_groups.extend(groups[bound])
    joined = defaultdict(set)
    for group, prior in zip(all_groups, old, strict=True):
        joined[int(group)].add(int(prior))
    merged = {key: value for key, value in joined.items() if len(value) > 1}
    protected = set(groups[boundary:])
    print({'rows': len(old), 'new_merges': len(merged),
           'local_overlap': len(set(all_groups) & protected), 'merged_examples': list(merged.items())[:5]})
    assert len(merged) == 8
    assert not set(all_groups) & protected
    from experiments.audio_lab_tools.uplift.uplift_gate_experiment import DEFAULT, datasets
    data = datasets(DEFAULT)
    fitted = np.concatenate([d['groups'] for d in data.values()])
    folds = np.concatenate([d['outer'] for d in data.values()])
    np.testing.assert_array_equal(fitted, all_groups)
    for group in set(fitted):
        assert len(set(folds[fitted == group])) == 1


def test_actual_fit_excludes_complete_outer_and_omitted_components():
    from experiments.audio_lab_tools.uplift.uplift_gate_experiment import DEFAULT, datasets, OMISSIONS
    data = datasets(DEFAULT)
    for path in DEFAULT.glob('*.trace.json'):
        stage = path.name.removesuffix('.trace.json')
        if stage.startswith('outer-'):
            held = {g for d in data.values() for g in d['groups'][d['outer'] == int(stage[-1])]}
        elif stage.startswith('transfer-'):
            held = {g for s in OMISSIONS[stage.removeprefix('transfer-')] for g in data[s]['groups']}
        else:
            held = set()
        trace = load(path)
        for head in [*trace['final_native'].values(), *trace['final_context'].values()]:
            assert not held & set(head['training_groups'])
        for fit in trace['crossfit'].values():
            valid = {g for value in fit['validation'].values() for g in value['groups']}
            for head in [*fit['native'].values(), *fit['arousal'].values()]:
                assert not (held | valid) & set(head['training_groups'])


def test_frozen_references_and_native_targets_remain_original():
    from experiments.audio_lab_tools.uplift.uplift_gate_experiment import DEFAULT, datasets
    data = datasets(DEFAULT)
    for source, d in data.items():
        prior = arrays(OLD / f'inputs/{source}.npz')
        for name in ('reference', 'y', 'native', 'ids', 'chunks', 'counts', 'common'):
            np.testing.assert_array_equal(d[name], prior[name])
        if source in (m.DEAM, m.PMEMO):
            names = load(DEFAULT / f'inputs/{source}.json')['target_names']
            np.testing.assert_array_equal(d['arousal'], d['native'][:, names.index('arousal')])


def test_receipts_and_support_rules():
    from experiments.audio_lab_tools.uplift.uplift_gate_experiment import DEFAULT
    from experiments.audio_lab_tools.support.quality_round import verify
    verify(DEFAULT, required_stages={
        'full', 'evaluation', *(f'outer-{i}' for i in range(5)),
        'transfer-soundtracks', 'transfer-memo', 'transfer-manymusic',
    }, optional_stages={'postrun-source'})
    report = load(DEFAULT / 'report.json')
    for preparations in report['contrasts'].values():
        for contrasts in preparations.values():
            for value in contrasts.values():
                assert value['supported'] == (value['rows'] >= 15 and value['groups'] >= 10)
                assert bool(value['metrics']) == value['supported']
