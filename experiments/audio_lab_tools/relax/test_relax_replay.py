"""Independent coefficient and blend replay on every retained #617 test row."""
import json
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2] / 'audio_lab/local/relax-617-20261030/round-1'


def load(path):
    return json.loads(path.read_text())


def native(head, x):
    if head['kind'] == 'constant':
        return np.full(len(x), head['value'][0])
    assert head['kind'] == 'ridge'
    value = np.full(len(x), head['intercept'][0])
    for j, coefficient in enumerate(head['weights']):
        value += (x[:, j] - head['mean'][j]) / head['scale'][j] * coefficient[0]
    return np.minimum(1, np.maximum(0, value))


def replay(model, x):
    expert = {}
    for name, value in model['experts'].items():
        predicted = {label: native(head, x) for label, head in zip(value['target_names'], value['heads'], strict=True)}
        if name == 'emotify-artist':
            expert[name] = (predicted['calmness'] + 1 - predicted['tension']) / 2
        elif name == 'soundtracks-all':
            expert[name] = (2 - predicted['energy'] - predicted['tension'] + predicted['tender']) / 3
        else:
            expert[name] = (1 - predicted['arousal'] + predicted['valence']) / 2
    if 'manymusic-natural' in expert:
        expert['manymusic'] = (expert.pop('manymusic-natural') + expert.pop('manymusic-generated')) / 2
    weights = {'soundtracks-all': .25, 'manymusic': .125, 'memo2496-pilot': .125}
    total = sum(weights[k] for k in expert if k != 'emotify-artist')
    fixed = .5 * expert['emotify-artist']
    for name, score in expert.items():
        if name != 'emotify-artist':
            fixed = fixed + .5 * weights[name] / total * score
    return {'anchor': expert['emotify-artist'], 'fixed': fixed,
            'learned': sum(expert[k] * w for k, w in zip(model['expert_names'], model['learned_weights'], strict=True))}


@pytest.mark.skipif(not (ROOT / 'evaluation.receipt.json').exists(), reason='Retained experiment unavailable')
def test_all_outer_and_transfer_predictions_reproduce_with_independent_arithmetic():
    total = 0
    for path in sorted((ROOT / 'models').glob('*.json')):
        if path.stem == 'full':
            continue
        model = load(path)
        with np.load(ROOT / (path.stem + '.npz'), allow_pickle=False) as saved:
            for key in saved.files:
                if not key.endswith('__ids'):
                    continue
                source = key.removesuffix('__ids')
                with np.load(ROOT / f'inputs/{source}.npz', allow_pickle=False) as data:
                    by_id = {name: i for i, name in enumerate(data['ids'])}
                    indices = [by_id[name] for name in saved[key]]
                    for prep, feature in (('historical', 'x'), ('browser', 'browser')):
                        if feature not in data:
                            continue
                        for method, predicted in replay(model, data[feature][indices]).items():
                            assert np.allclose(predicted, saved[f'{source}__{prep}__{method}'], rtol=0, atol=1e-12)
                            total += len(predicted)
    assert total > 10000
