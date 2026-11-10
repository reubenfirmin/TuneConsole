"""Independent replay of frozen heads against all actual pilot predictions."""
import numpy as np

from experiments.audio_lab_tools.studies.midlevel_pilot import DEFAULT, PROPERTIES, predict
from experiments.audio_lab_tools.focus.focus_experiment import arrays, load


def test_serialized_heads_reproduce_every_outer_prediction():
    data = arrays(DEFAULT / 'inputs/data.npz')
    expected = arrays(DEFAULT / 'predictions.npz')
    for fold in range(5):
        test = np.flatnonzero(data['outer'] == fold)
        train = np.flatnonzero(data['outer'] != fold)
        for variant in ('direct', 'bottleneck', 'augmented', 'oracle'):
            model = load(DEFAULT / f'models/{variant}-{fold}.json')
            assert model['training_rows'] == train.tolist()
            assert model['test_rows'] == test.tolist()
            prop = predict(model['auxiliary'], data['x'][test])
            x = {'direct': data['x'][test], 'bottleneck': prop,
                 'augmented': np.column_stack([data['x'][test], prop]),
                 'oracle': data['properties'][test]}[variant]
            np.testing.assert_array_equal(predict(model['heads'], x), expected[variant][test])
            np.testing.assert_array_equal(prop, expected['property_prediction'][test])
    assert len(PROPERTIES) == 7
