"""Actual paired chroma cohort, fold exclusions and completed receipts."""
import os
import subprocess

import numpy as np

from experiments.audio_lab_tools.chroma_study import DEFAULT, PYTHON, ROOT
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import verify


def test_chroma_preserves_known_pitch_and_silence():
    script = '''
import numpy as np
import librosa
from experiments.audio_lab_tools.chroma_worker import summarize
t = np.arange(160000) / 16000
kwargs = dict(sr=16000, hop_length=512, fmin=32.70319566257483,
    n_chroma=12, n_octaves=7, bins_per_octave=36, tuning=0.,
    norm=np.inf, threshold=0., cqt_mode='full')
silence = summarize(librosa.feature.chroma_cqt(y=np.zeros(160000), **kwargs))
assert silence.shape == (72,) and not np.any(silence)
tone = summarize(librosa.feature.chroma_cqt(y=np.sin(2*np.pi*261.625565*t), **kwargs))
assert tone.shape == (72,) and np.isfinite(tone).all()
assert tone[:12].argmax() == 0
assert tone[0] > .9 and tone[6] < .1
'''
    env = {**os.environ, 'NUMBA_CACHE_DIR': str(ROOT / 'numba-cache'),
           **{k: '1' for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                               'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS')}}
    result = subprocess.run([str(PYTHON), '-B', '-c', script], env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_chroma_alignment_and_unchanged_control():
    data = arrays(DEFAULT / 'features.npz')
    old = arrays(DEFAULT / 'inputs/baseline-features.npz')
    rows = load(DEFAULT / 'extraction.json')['rows']
    assert len(rows) == 288 and sum(data['counts']) == 576
    np.testing.assert_array_equal(data['musicnn'], old['musicnn'])
    np.testing.assert_array_equal(data['counts'], old['counts'])
    np.testing.assert_array_equal(data['combined'][:, :, :200], data['musicnn'])
    np.testing.assert_array_equal(data['combined'][:, :, 200:], data['chroma'])
    assert data['chroma'].shape == (288, 6, 72)
    assert np.isfinite(data['chroma']).all()
    for row, n in zip(data['chroma'], data['counts'], strict=True):
        assert not np.any(row[n:])
        assert np.any(row[:n])
    baseline = arrays(DEFAULT / 'inputs/baseline-predictions.npz')
    result = arrays(DEFAULT / 'predictions.npz')
    for key, value in baseline.items():
        np.testing.assert_array_equal(result[key], value)


def test_exclusions_replay_and_all_gates_recorded():
    assert verify(DEFAULT, required_stages=['extraction', 'predictions', 'evaluation'])['receipts'] == 3
    rows = load(DEFAULT / 'extraction.json')['rows']
    traces = load(DEFAULT / 'fit-trace.json')['contexts']
    assert len(traces) == 26
    for name, trace in traces.items():
        if '__outer-' in name:
            fold = int(name.rsplit('-', 1)[1])
            expected = {r['component'] for r in rows if r['outer'] == fold}
        else:
            expected = {r['component'] for r in rows if r['source'] in trace['omitted_sources']}
        assert set(trace['protected_components']) == expected
        for source, detail in trace['sources'].items():
            assert source not in trace['omitted_sources']
            assert not expected.intersection(detail['training_groups'])
            for g in set(detail['training_groups']):
                assert len({f for k, f in zip(detail['training_groups'], detail['inner_folds'], strict=True) if k == g}) == 1
    extraction = load(DEFAULT / 'extraction.json')
    assert len(extraction['audit']) == 576
    assert max(r['maximum_error'] for r in extraction['resources']['replay']) <= 1e-6
    report = load(DEFAULT / 'report.json')
    assert len(report['decision']) == 12 and len(report['native']) == 24
    assert report['scope']['new_memo_used'] == 0
    assert not report['resource_failures']
