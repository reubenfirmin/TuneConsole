"""Checks for common-target aggregation and corrected real-data isolation."""
import numpy as np
import pytest

from experiments.audio_lab_tools.studies.candidate_followup import average_chunks
from experiments.audio_lab_tools.studies.mapped_vibe_rules import apply, registry


def test_native_scores_are_averaged_before_nonlinear_primary_mapping():
    # Same rated interval, opposite activation at its two ends. Averaging
    # per-chunk moderate-activation scores answers a different question.
    native = np.array([[.8, .1], [.8, .9], [.2, .3]])
    offsets = np.array([0, 2, 3])
    expression = registry()['pmemo']['cruise']['expression']
    names = ['valence', 'arousal']
    primary = apply(expression, average_chunks(native, offsets), names)
    secondary = average_chunks(apply(expression, native, names), offsets)
    np.testing.assert_allclose(primary, [.9, .4])
    np.testing.assert_allclose(secondary, [.5, .4])
    assert len(primary) == 2  # recordings, not three independent chunk votes.


def test_corrected_reports_remove_independently_audited_component_remainders():
    # Reuse the reviewer's independent, metadata-only audit implementation.
    # This checks actual replacement fit exclusions, not just a toy graph.
    from experiments.audio_lab_tools.studies.test_mapped_transfer_identity_audit import _direction
    from experiments.audio_lab_tools.evaluation.transfer_component_repair import DEFAULT, BASE, BASE_SHA256
    from experiments.audio_lab_tools.studies.vibe_specialist_round import load, receipt_valid
    from experiments.audio_lab_tools.support.external_experiment import digest
    if not (DEFAULT / 'summary.receipt.json').exists():
        pytest.skip('Requires the separately retained local corrected transfer artifacts')
    plan = load(DEFAULT / 'plan.json')
    corrected = load(DEFAULT / 'summary.json')
    changed = set()
    for source, target in load(BASE / 'plan.json', BASE_SHA256)['transfer']:
        source_meta = load(BASE / f'inputs/{source}.json')
        target_meta = load(BASE / f'inputs/{target}.json')
        audit = _direction(source_meta, target_meta)
        expected = set(audit['direct_excluded_ids'])
        for component in audit['connected_identity_remainders']:
            expected.update(component['retained_ids'])
        key = source + '__' + target
        origin = DEFAULT if [source, target] in plan['transfer'] else BASE
        saved = load(origin / f'transfer-{key}.report.json')
        assert set(saved['excluded_ids']) == expected, key
        assert saved['training_rows'] == len(source_meta['rows']) - len(expected), key
        remaining = [row for row in source_meta['rows'] if row['id'] not in expected]
        qualified = _direction({'rows': remaining, 'groups': list(range(len(remaining)))}, target_meta)
        assert qualified['direct_excluded_ids'] == [], key
        assert qualified['connected_retained_rows'] == 0, key
        assert corrected['transfer_lineage'][key]['report_sha256'] == digest(origin / f'transfer-{key}.report.json')
        if audit['connected_retained_rows']:
            changed.add(key)
    assert changed == {'pmemo__amg1608-matched', 'pmemo__ntwicm-matched'}
    assert receipt_valid(DEFAULT, 'summary', digest(DEFAULT / 'plan.json'))
    assert load(DEFAULT / 'summary.json')['original_evidence_unchanged'] is True
