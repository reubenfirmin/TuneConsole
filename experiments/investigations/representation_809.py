"""Actual-data acceptance evidence for fixed intermediate MERT layers."""
from collections import defaultdict
import numpy as np

from experiments.audio_lab_tools.representation_study import DEFAULT, ORIGINAL
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import validate, verify


def test_frozen_cohort_and_native_intervals():
    plan = validate(DEFAULT)
    assert plan['ticket'] == 809
    assert 'audio_lab_tools/representation_study.py' in plan['code']
    rows = load(DEFAULT / 'inputs/baseline-rows.json')['rows']
    assert rows == load(ORIGINAL / 'extraction.json')['rows']
    assert len(rows) == 288 and sum(len(r['chunk_starts']) for r in rows) == 576
    original = arrays(DEFAULT / 'inputs/baseline-features.npz')
    actual = arrays(DEFAULT / 'features.npz')
    np.testing.assert_array_equal(actual['musicnn'], original['musicnn'])
    np.testing.assert_array_equal(actual['mert'], original['mert'])
    np.testing.assert_array_equal(actual['counts'], original['counts'])
    extraction = load(DEFAULT / 'extraction.json')
    assert len(extraction['audit']) == 576
    assert extraction['resources']['final_layer_max_error'] <= 1e-6
    assert extraction['resources']['replay_max_error'] <= 1e-6
    assert actual['middle'].shape == (288, 6, 1536)
    for row, count in zip(actual['middle'], actual['counts'], strict=True):
        assert np.isfinite(row[:count]).all()
        assert not np.any(row[count:])
        assert not np.allclose(row[0, :768], row[0, 768:])


def test_global_exclusions_and_completed_evidence():
    rows = load(DEFAULT / 'extraction.json')['rows']
    traces = load(DEFAULT / 'fit-trace.json')['contexts']
    assert len(traces) == 13
    for name, context in traces.items():
        protected = set(context['protected_components'])
        if '__outer-' in name:
            fold = int(name.rsplit('-', 1)[1])
            assert protected == {r['component'] for r in rows if r['outer'] == fold}
        else:
            omitted = context['omitted_sources']
            assert protected == {r['component'] for r in rows if r['source'] in omitted}
            assert not (set(omitted) & context['sources'].keys())
        for source, trace in context['sources'].items():
            assert not (protected & set(trace['training_groups']))
            identities = {r['id']: r for r in rows if r['source'] == source}
            assert trace['training_groups'] == [identities[k]['component'] for k in trace['training_ids']]
            folds = defaultdict(set)
            for group, fold in zip(trace['training_groups'], trace['inner_folds'], strict=True):
                folds[group].add(fold)
            assert all(len(v) == 1 for v in folds.values())
    assert verify(DEFAULT)['receipts'] == 3
    report = load(DEFAULT / 'report.json')
    assert report['scope']['new_memo_used'] == 0
    assert len(report['decision']) == 6 and len(report['native']) == 24
    assert not report['resource_failures']
