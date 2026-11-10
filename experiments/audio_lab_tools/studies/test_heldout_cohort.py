"""Verify the frozen evaluation against serving math without revealing labels."""
import json

import numpy as np
import pytest

from experiments.audio_lab_tools.studies import heldout_cohort as cohort
from yt_playlist.rec.audio_vibe_external import _predict, validate


def test_checkpoint_replay_matches_serving_on_real_historical_embeddings():
    model = validate(json.loads(cohort.models()['checkpoint'].read_text()))
    rows = json.loads(cohort.SNAPSHOT.read_text())['gold']
    x = np.asarray([row['embedding_values'] for row in rows if len(row.get('embedding_values', [])) == 200])
    expected = []
    for values in x:
        native = _predict(model['native'], values.tolist())
        output = np.maximum(_predict(model['calibration'], native), 1e-8)
        expected.append(output / output.sum())
    actual = cohort.candidate_predictions(model, x)['prediction']
    assert actual.shape == (55, 6)
    assert np.max(np.abs(actual - expected)) < 1e-12


def test_completion_receipts_reject_an_altered_model_before_scoring(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort, 'ROOT', tmp_path)
    monkeypatch.setattr(cohort, 'STEPS', ('example',))
    path = tmp_path / 'model.json'
    path.write_text('{"weight": 1}')
    state = {'steps': [{'name': 'example', 'state': 'complete', 'fingerprint': 'frozen',
                        'outputs': {'model.json': cohort.digest(path)}}]}
    assert cohort.completed_receipts(state)['example']['fingerprint'] == 'frozen'
    path.write_text('{"weight": 2}')
    with pytest.raises(ValueError, match='outputs changed'):
        cohort.completed_receipts(state)


def test_paired_uncertainty_keeps_shared_artist_rows_together():
    y = np.eye(6)[[0, 0, 1]]
    baseline = np.full((3, 6), 1 / 6)
    candidate = baseline.copy()
    candidate[2] = [.01, .95, .01, .01, .01, .01]
    result = cohort.paired_bootstrap(y, baseline, candidate, ['artist-a', 'artist-a', 'artist-b'])
    change = result['acceptable_top_choice_gain']
    assert change['difference'] == pytest.approx(1 / 3)
    assert change['conditional_95_percent_interval'] == [0., 1.]
