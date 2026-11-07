"""Saved-evidence checks for #707, independent of its fitting implementation.

Run after extraction/evaluation using ./experiments/run.py investigate
many_windows_707.py. These checks use actual frozen research rows.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1] / 'audio_lab/local/many-windows-707-20261101/round-5'


def read(path):
    return json.loads(path.read_text())


def arrays(path):
    with np.load(path, allow_pickle=False) as value:
        return {name: value[name] for name in value.files}


def test_available_overlap_reproduces_pilot_features():
    expected = {}
    for source in ('manymusic-natural', 'manymusic-generated'):
        meta = read(ROOT / f'inputs/{source}.json')
        data = arrays(ROOT / f'inputs/{source}.npz')
        for row, start, stop in zip(meta['rows'], data['offsets'][:-1], data['offsets'][1:], strict=True):
            for offset, vector in enumerate(data['embedding'][start:stop]):
                expected[row['audio_sha256'], row['annotation_start'] + offset * 10] = vector
    checked = 0
    for path in ROOT.glob('worker-*/chunks/*.json'):
        record = read(path)
        identity = record['input']
        key = identity['audio_sha256'], identity['start']
        if key in expected:
            np.testing.assert_allclose(record['result']['embedding'], expected[key], atol=.0005, rtol=0)
            checked += 1
    assert checked >= 3


def test_frozen_components_keep_recording_artist_prompt_and_audio_links():
    seen = {}
    for row in read(ROOT / 'inputs/window-plan.json')['rows']:
        identities = [('recording', row['recording_id']), ('audio', row['audio_sha256']),
                      ('declared', row['group'])]
        if row['synthetic_audio']:
            identities.append(('prompt', row['prompt_id']))
        elif row.get('artist_id'):
            identities.append(('jamendo_artist', row['artist_id']))
        for key in identities:
            if key[1] is not None:
                assert seen.setdefault(key, row['component']) == row['component']


def test_all_windows_accounted_for_without_extra_independent_recordings():
    planned = read(ROOT / 'inputs/window-plan.json')['rows']
    extracted = read(ROOT / 'extraction.json')
    data = arrays(ROOT / 'windows.npz')
    assert len(planned) == 1695
    assert len({r['recording_id'] for r in planned}) == 283
    retained = {r['id'] for r in extracted['rows']}
    rejected = {r['id'] for r in extracted['rejected']}
    assert not retained & rejected
    assert retained | rejected == {r['id'] for r in planned}
    assert set(data['ids']) == retained
    assert data['chunks'].shape == (len(retained), 3, 200)
    assert np.isfinite(data['chunks']).all()
    np.testing.assert_allclose(data['x'], data['chunks'].mean(axis=1), atol=1e-12)
    for row, target in zip(extracted['rows'], data['y'], strict=True):
        expected = [(row['targets'][row['target_names'].index(k)] + 1) / 2
                    for k in ('valence', 'arousal')]
        np.testing.assert_allclose(target, expected, atol=1e-12)


@pytest.mark.parametrize('source,synthetic', [('manymusic-natural', False), ('manymusic-generated', True)])
def test_original_selected_audio_and_targets_reproduced(source, synthetic):
    new = arrays(ROOT / 'windows.npz')
    old = arrays(ROOT / f'inputs/{source}.npz')
    old_ids = [r['id'] for r in read(ROOT / f'inputs/{source}.json')['rows']]
    keep = new['selected'] & (new['synthetic'] == synthetic)
    for i in np.flatnonzero(keep):
        position = old_ids.index(new['ids'][i])
        start, end = old['offsets'][position:position + 2]
        assert end - start == 3
        np.testing.assert_allclose(new['chunks'][i], old['embedding'][start:end], atol=.0005, rtol=0)
        np.testing.assert_allclose(new['y'][i], old['y'][position], atol=1e-12, rtol=0)


def test_fitted_components_and_recording_weights_match_frozen_partitions():
    data = arrays(ROOT / 'windows.npz')
    evaluated = arrays(ROOT / 'predictions.npz')
    for path in ROOT.glob('*.trace.json'):
        traces = read(path)
        for name, trace in traces.items():
            assert name in ('one', 'all')
            train = set(trace['training_groups'])
            assert set(trace['fit_groups']) <= train
            for inner in trace['selection']:
                assert not set(inner['training_groups']) & set(inner['validation_groups'])
            ids = trace['training_ids']
            positions = np.array([np.flatnonzero(data['ids'] == identity)[0] for identity in ids])
            used = positions if name == 'all' else positions[data['selected'][positions]]
            recordings, counts = np.unique(data['recordings'][used], return_counts=True)
            assert len(recordings) == trace['fit_recordings']
            assert len(used) == trace['fit_windows']
            assert np.all(counts > 0)
            model_path = path.with_name(path.name.replace('.trace.json', '.model.json'))
            model = read(model_path)[name]
            weights = np.array([1 / counts[np.flatnonzero(recordings == recording)[0]]
                                for recording in data['recordings'][used]])
            np.testing.assert_allclose(model['mean'], np.average(data['x'][used], axis=0, weights=weights), atol=1e-12)
            np.testing.assert_allclose(model['intercept'], np.average(data['y'][used], axis=0, weights=weights), atol=1e-12)
            if '-outer-' in path.name:
                kind, suffix = path.name.split('-outer-')
                fold = int(suffix.split('.')[0])
                prefix = kind + '__historical__cruise__'
                forbidden = set(evaluated[prefix + 'groups'][evaluated[prefix + 'outer'] == fold])
                assert not train & forbidden
            else:
                destination = path.name.split('-transfer-')[1].split('.trace')[0]
                forbidden = read(ROOT / 'inputs/window-plan.json')['components'][destination]
                assert not train & set(forbidden)


def test_receipts_cover_exact_artifacts():
    from experiments.audio_lab_tools.support.quality_round import verify
    verify(ROOT, required_stages={
        'extraction', 'evaluation',
        *(f'{kind}-outer-{fold}' for kind in ('natural', 'generated') for fold in range(5)),
        *(f'{kind}-transfer-{source}' for kind in ('natural', 'generated')
          for source in ('soundtracks-all', 'memo2496-pilot')),
    }, optional_stages={'postrun-source'})
    plan_hash = hashlib.sha256((ROOT / 'plan.json').read_bytes()).hexdigest()
    receipts = list(ROOT.glob('*.receipt.json'))
    for path in receipts:
        receipt = read(path)
        assert receipt['plan_sha256'] == plan_hash
        for relative, expected in receipt['outputs'].items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
