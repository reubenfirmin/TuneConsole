"""Independent coefficient, clip, excerpt and blend replay for #618."""
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2] / 'audio_lab/local/uplift-618-20261030/round-1'


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


def replay(model, chunks, counts):
    expert = {}
    for name, value in model['experts'].items():
        # Each expert's Uplift map is a mean of its explicitly named native heads.
        # Soundtracks has valence/happy; every other source has one native target.
        expert[name] = np.array([np.mean([native(head, row[:count]).mean() for head in value['heads']])
                                 for row, count in zip(chunks, counts, strict=True)])
    if 'manymusic-natural' in expert:
        expert['manymusic'] = (expert.pop('manymusic-natural') + expert.pop('manymusic-generated')) / 2
    va = ('deam', 'pmemo', 'manymusic', 'memo2496-pilot')
    emotion = {'soundtracks-all': .25, 'jamendo-human-4shards': .125, 'emotify-artist': .125}
    present = [s for s in expert if s in va]
    emotion_total = sum(w for s, w in emotion.items() if s in expert)
    fixed = sum(.5 / len(present) * expert[s] for s in present)
    fixed += sum(.5 * w / emotion_total * expert[s] for s, w in emotion.items() if s in expert)
    return {'anchor': expert['deam'], 'fixed': fixed,
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
                    for prep, feature in (('historical', 'chunks'), ('browser', 'browser')):
                        if feature not in data:
                            continue
                        for method, predicted in replay(model, data[feature][indices], data['counts'][indices]).items():
                            assert np.allclose(predicted, saved[f'{source}__{prep}__{method}'], rtol=0, atol=1e-12)
                            total += len(predicted)
    assert total > 15000
