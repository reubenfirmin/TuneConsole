"""Guard evidence integrity and target-independent reference selection for #424."""
import copy

import numpy as np
import pytest

from experiments.audio_lab_tools import audio_evaluation as evaluation


def test_selection_is_independent_of_targets_and_keeps_recordings_distinct():
    rows = [{'id': str(i), 'recording_id': str(i // 2), 'targets': [i / 20]} for i in range(20)]
    before = evaluation.unique(rows, 6)
    changed = copy.deepcopy(rows)
    for row in changed:
        row['targets'] = [1 - row['targets'][0]]
    after = evaluation.unique(list(reversed(changed)), 6)
    assert [r['id'] for r in before] == [r['id'] for r in after]
    assert len({r['recording_id'] for r in before}) == 6
    with pytest.raises(ValueError, match='Insufficient distinct'):
        evaluation.unique(rows, 11)


def frozen(tmp_path):
    source = tmp_path / 'source'
    snapshot = tmp_path / 'snapshot'
    source.write_bytes(b'real input identity')
    snapshot.write_bytes(b'packaged runtime')
    plan = {'budgets': evaluation.BUDGETS, 'inputs': [evaluation.pinned(source)],
            'snapshot': [evaluation.pinned(snapshot)], 'cases': [], 'hardware': {'fixture': True}}
    evaluation.save(tmp_path / 'plan.json', plan)
    evaluation.save(tmp_path / 'plan.sha256.json', {'sha256': evaluation.digest(tmp_path / 'plan.json')})
    return source, snapshot


@pytest.mark.parametrize('changed', ['source', 'snapshot', 'plan.json'])
def test_reuse_rejects_mutated_inputs_runtime_or_plan(tmp_path, changed):
    frozen(tmp_path)
    evaluation.validate(tmp_path)
    (tmp_path / changed).write_bytes(b'changed')
    with pytest.raises(ValueError):
        evaluation.validate(tmp_path)


def test_verified_receipt_rejects_changed_result(tmp_path):
    frozen(tmp_path)
    target = tmp_path / 'retained-decode'
    target.mkdir()
    evaluation.receipt(tmp_path, target, {'cases': []})
    evaluation.verify(tmp_path)
    (target / 'report.json').write_text('{"cases": ["invented"]}')
    with pytest.raises(ValueError, match='Pinned input changed'):
        evaluation.verify(tmp_path)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, .001])
def test_numerical_gate_rejects_invalid_or_out_of_budget_embedding(bad):
    assert not evaluation.numerical_pass({'embedding': bad, 'tags': 0, 'moods': 0})


def test_waveform_comparison_handles_silence_and_rejects_missing_or_invalid_samples():
    silence = np.zeros(160000)
    result = evaluation.waveform(silence, silence)
    assert result['rms_delta'] == 0
    assert result['projected_amplitude_ratio'] is None
    with pytest.raises(ValueError, match='ten-second'):
        evaluation.waveform(silence, silence[:-1])
    broken = silence.copy()
    broken[100] = float('nan')
    with pytest.raises(ValueError, match='finite'):
        evaluation.waveform(silence, broken)
