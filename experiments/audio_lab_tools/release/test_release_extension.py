"""Target-independent partition and native-target opening checks."""
import numpy as np

from experiments.audio_lab_tools.release import release_extension as m
from experiments.audio_lab_tools.studies.mapped_vibe_inference import replay


def test_component_partition_preserves_artist_units_and_ignores_outcomes():
    rows = [{'id': str(i), 'component': i // 2, 'source_artist_id': str(i // 2), 'target': i / 20} for i in range(20)]
    first = {r['id']: r['partition'] for r in m.artist_partition(rows)}
    second = {r['id']: r['partition'] for r in m.artist_partition([{**r, 'target': 1-r['target']} for r in reversed(rows)])}
    assert first == second
    for row in rows:
        assert first[str(2 * row['component'])] == first[str(2 * row['component'] + 1)]
    assert list(first.values()).count('train') == 10


def test_unrequested_target_rows_remain_unparsed(tmp_path):
    path = tmp_path / 'annotations.tsv'
    path.write_text('header\ntrain\ta\tb\tp\t12\tmood_aggressive---aggressive,not_aggressive\nheld\ta\tb\tp\t12\tmalformed unopened target\n')
    assert m.read_targets(path, ['train']) == {'train': {'target': .5, 'responses': 2}}


def test_native_alpha_and_independent_replay():
    rng = np.random.default_rng(709)
    x, y = rng.normal(size=(30, 4)), rng.uniform(size=30)
    head = m.fit_head(x, y)
    assert head['alpha'] == 1000
    transformed = (x - np.asarray(head['mean'])) / np.asarray(head['scale'])
    expected = np.clip(transformed @ np.asarray(head['weights']) + np.asarray(head['intercept']), 0, 1)
    np.testing.assert_allclose(replay(head, x), expected, atol=1e-12)


def test_empty_acquisition_can_seal_predictions(tmp_path, monkeypatch):
    from experiments.audio_lab_tools.support.external_experiment import save
    rng = np.random.default_rng(709)
    x, y = rng.normal(size=(30, 4)), rng.uniform(size=(30, 1))
    for source, label in ((m.JAM, 'aggressive'), (m.SOUND, 'anger')):
        path = tmp_path / f'inputs/{source}.npz'
        path.parent.mkdir(exist_ok=True)
        np.savez(path, embedding=x, browser=x, y=y)
        save(tmp_path / f'inputs/{source}.json', {'target_names': [label], 'rows': [{'id': str(i)} for i in range(30)]})
    save(tmp_path / 'inputs/selection.json', {'components': {m.JAM: list(range(30)), m.SOUND: list(range(30,60))}, 'local_components': []})
    save(tmp_path / 'inputs/adopted.json', {'model': m.fit_head(x, y[:, 0])})
    save(tmp_path / 'extraction.json', {'rows': []})
    np.savez(tmp_path / 'features.npz', x=np.empty((0,4)))
    (tmp_path / 'inputs/annotations.tsv').write_text('header\n')
    monkeypatch.setattr(m, 'check_receipt', lambda *args: None)
    monkeypatch.setattr(m.evidence, 'seal', lambda *args: None)
    m.predict(tmp_path)
    result = np.load(tmp_path / 'predictions.npz')
    assert result['reserved__historical__extended'].shape == (0,)
    np.testing.assert_allclose(result['soundtracks__historical__extended'], result['soundtracks__historical__original'])
