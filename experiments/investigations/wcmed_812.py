"""Actual source identities, interval coverage and frozen model replay."""
import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.studies.wcmed_study import DEFAULT, components, VIBES, va_scores
from experiments.audio_lab_tools.focus.focus_experiment import arrays, load
from experiments.audio_lab_tools.support.quality_baselines import predict
from experiments.audio_lab_tools.support.quality_round import verify


def test_actual_source_identity_and_every_interval():
    assert verify(DEFAULT, required_stages=['acquisition','features','predictions','evaluation'])['receipts'] == 4
    acquired = load(DEFAULT / 'acquisition.json')
    rows = acquired['rows']
    assert len(rows)+len(acquired['excluded']) == 400
    assert len({r['id'] for r in rows}) == len(rows)
    np.testing.assert_array_equal(components(rows), [r['component'] for r in rows])
    for row in rows:
        assert row['sample_start'] >= float(row['start_point'])
        assert row['sample_start']+10 <= float(row['stop_point'])+.000001
        source = acquired['sources'][row['songName']]
        assert row['sample_start']+10 <= source['duration']
        assert source['final_url'].endswith(row['songName']+'.mp3')
        assert row['audio_sha256'] == source['audio_sha256']
    assert acquired['received_bytes'] <= 1024**3


def test_all_frozen_specialists_replay_actual_vectors():
    x = arrays(DEFAULT / 'features.npz')['x']
    predictions = arrays(DEFAULT / 'predictions.npz')
    assert x.shape[1] == 200 and np.isfinite(x).all()
    with threadpool_limits(limits=1):
        for vibe in VIBES:
            score, _ = predict(load(DEFAULT / f'inputs/adopted-{vibe}.json'),x[:,None,:],np.ones(len(x),dtype=int))
            np.testing.assert_array_equal(score,predictions['adopted-'+vibe])
    assert set(va_scores(predictions['prior-va'])) == set(VIBES)
