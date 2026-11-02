"""Independent recording weights, exclusion and exact-baseline replay checks."""
import importlib
from pathlib import Path

import numpy as np
import pytest

from experiments.audio_lab_tools import many_window_models as m, quality_baselines
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_audio import recording_weights


def test_repeated_windows_do_not_increase_recording_weight():
    ids = np.array(['a', 'a', 'a', 'b', 'c', 'c'])
    weights = recording_weights(ids)
    for recording in set(ids):
        assert weights[ids == recording].sum() == 1


def test_recording_balanced_fit_unchanged_by_repeating_a_window():
    x, y, ids = np.array([[0., 2.], [1., 4.], [3., 5.]]), np.array([[.1, .2], [.5, .6], [.9, .8]]), np.array(['a', 'b', 'c'])
    one = m.fit(x, y, ids, 10.)
    repeated = m.fit(x[[0, 0, 0, 1, 2]], y[[0, 0, 0, 1, 2]], ids[[0, 0, 0, 1, 2]], 10.)
    np.testing.assert_allclose(m.predict(one, x), m.predict(repeated, x), atol=1e-12)


def test_training_groups_never_include_validation_components():
    rng = np.random.default_rng(707)
    groups = np.repeat(np.arange(12), 2)
    data = {'x': rng.normal(size=(24, 4)), 'y': rng.uniform(size=(24, 2)), 'groups': groups,
            'selected': np.tile([True, False], 12), 'ids': np.array([str(i) for i in range(24)]),
            'recordings': groups.astype(str)}
    model, trace = m.select(data, groups < 10, True)
    assert set(trace['fit_groups']) == set(range(10))
    for stage in trace['selection']:
        assert not set(stage['training_groups']) & set(stage['validation_groups'])
    assert trace['fit_windows'] == 20 and trace['fit_recordings'] == 10


@pytest.mark.parametrize('vibe', ['cruise', 'relax', 'uplift', 'energize'])
def test_independent_replay_matches_installed_specialist(vibe):
    runtime = importlib.import_module('yt_playlist.rec.audio_vibe_' + vibe)
    root = Path(__file__).resolve().parents[1] / 'audio_lab/local/individual-20261030/remaining-1/inputs'
    x = arrays(root / 'memo2496-pilot.npz')['embedding'][:18].reshape(3, 6, 200)
    expected = [runtime.score(runtime.load(runtime.DEFAULT_PATH), values.tolist())['score'] for values in x]
    actual, _ = quality_baselines.predict(load(runtime.DEFAULT_PATH), x, np.full(3, 6))
    np.testing.assert_allclose(actual, expected, atol=1e-12, rtol=0)
