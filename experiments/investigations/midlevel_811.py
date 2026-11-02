"""Independent actual-cohort checks of every nested auxiliary exclusion."""
import numpy as np

from experiments.audio_lab_tools.midlevel_pilot import DEFAULT, ACQUISITION
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import verify


def test_acquisition_and_actual_property_cohort():
    assert verify(ACQUISITION, required_stages=['acquisition'])['receipts'] == 1
    assert verify(DEFAULT, required_stages=['predictions', 'evaluation'])['receipts'] == 2
    audit = load(DEFAULT / 'inputs/audit.json')
    data = arrays(DEFAULT / 'inputs/data.npz')
    assert len(audit['rows']) == 360 and len({r['midlevel_id'] for r in audit['rows']}) == 360
    assert all(r['id'].startswith('soundtracks:set1:') for r in audit['rows'])
    assert audit['new_memo_used'] == audit['new_audio_bytes'] == 0
    assert data['x'].shape == (360, 200) and data['properties'].shape == (360, 7)
    assert data['properties'].min() >= 0 and data['properties'].max() <= 1
    assert len(set(data['groups'])) == audit['components'] >= 12
    for field in ('audio_sha256', 'film', 'legacy_component'):
        values = {r[field] for r in audit['rows'] if r[field] != ''}
        for value in values:
            ids = [i for i, r in enumerate(audit['rows']) if r[field] == value]
            assert len(set(data['outer'][ids])) == 1


def test_auxiliary_outer_and_inner_protection():
    data = arrays(DEFAULT / 'inputs/data.npz')
    traces = load(DEFAULT / 'fit-trace.json')['contexts']
    assert len(traces) == 80
    for name, trace in traces.items():
        fold = int(name.split('/')[0].split('-')[1])
        protected = set(trace['protected_components'])
        assert protected.issuperset(set(data['groups'][data['outer'] == fold]))
        assert not protected.intersection(trace['training_components'])
        assert not protected.intersection(data['groups'][trace['train']])
        for group in set(trace['training_components']):
            assert len({f for g, f in zip(trace['training_components'], trace['inner_folds'], strict=True) if g == group}) == 1
    report = load(DEFAULT / 'report.json')
    assert len(report['properties']) == 7 and len(report['emotion']) == 8
    assert 'focus' not in report['vibes']
    pred = arrays(DEFAULT / 'predictions.npz')
    assert all(np.isfinite(value).all() for value in pred.values())
