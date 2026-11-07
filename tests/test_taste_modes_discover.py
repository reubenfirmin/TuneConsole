import numpy as np
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from yt_playlist.rec import taste_modes as tm


def test_mode_label_single_and_blended():
    assert tm.mode_label([("house", 80), ("techno", 10)]) == "house"
    assert tm.mode_label([("house", 80), ("techno", 50)]) == "house + techno"
    assert tm.mode_label([("house", 80)]) == "house"


def _planted(n_per=50, d=6):
    rng = np.random.Generator(np.random.PCG64(1))
    keys, rows = [], []
    blocks = {"house": [1, 0, 0, 0, 0, 0], "rock": [0, 1, 0, 0, 0, 0], "jazz": [0, 0, 1, 0, 0, 0]}
    for fam, base in blocks.items():
        for i in range(n_per):
            v = np.array(base, dtype=np.float64) + rng.normal(0, 0.02, size=d)
            v /= np.linalg.norm(v)
            keys.append(f"{fam}|{i}")
            rows.append(v.astype(np.float32))
    V = np.stack(rows)
    return keys, V, {k: i for i, k in enumerate(keys)}, blocks


def _reader():
    model = Mock(spec=('content_generation',))
    model.content_generation.return_value = {'model': None}
    genres = Mock(spec=('feature_axes',))
    genres.feature_axes.side_effect = lambda labels: {label: (label, label) for label in labels}
    modes = Mock(spec=('genres_for',))
    modes.genres_for.side_effect = lambda keys: {key: key.split('|')[0] for key in keys}
    settings = Mock(spec=('get_setting',))
    settings.get_setting.return_value = None
    return SimpleNamespace(rec_model=model, modes=modes, genres=genres, settings=settings)


def test_discover_modes_finds_planted():
    keys, V, idx, blocks = _planted()
    store = _reader()
    # k=3 matches the 3 trivially-separable planted blocks, so recovery is seed-robust (k>3 would let
    # k-means legitimately split a tight blob into two dense clusters for some seeds).
    modes = tm.discover_modes(store, k=3, min_members=20, n_rep=3, vector_space=(keys, V, idx))
    store.genres.feature_axes.assert_called_once()
    assert sorted(store.genres.feature_axes.call_args.args[0]) == sorted(key.split('|')[0] for key in keys)
    assert len(modes) == 3
    labels = sorted(m["label"] for m in modes)
    assert labels == ["house", "jazz", "rock"]
    for m in modes:
        assert m["size"] >= 20
        assert np.isclose(np.linalg.norm(m["centroid"]), 1.0, atol=1e-5)
        assert len(m["rep_keys"]) == 3
        # representative keys belong to the same planted block as the label
        assert all(k.split("|")[0] == m["label"] for k in m["rep_keys"])


def test_discover_modes_cold_start():
    store = _reader()
    assert tm.discover_modes(store, vector_space=(
        ['a|1'], np.ones((1, 4), dtype=np.float32), {'a|1': 0})) == []
    store.genres.feature_axes.assert_not_called()


@pytest.mark.parametrize('count', [79, 80], ids=['below-admission', 'at-admission'])
def test_mode_fit_keeps_the_eighty_recording_admission_boundary(count):
    keys = [f'jazz|{index}' for index in range(count)]
    matrix = np.tile(np.array([1., 0.], dtype=np.float32), (count, 1))
    store = _reader()
    modes = tm.discover_modes(store, k=1, min_members=8,
        vector_space=(keys, matrix, {key: index for index, key in enumerate(keys)}))
    if count == 79:
        assert modes == []
        store.genres.feature_axes.assert_not_called()
    else:
        assert len(modes) == 1
        mode = modes[0]
        assert mode['size'] == 80
        assert mode['member_keys'] == keys
        assert mode['families'] == [('jazz', 80)]
        assert set(mode['rep_keys']) <= set(keys)
        assert len(mode['rep_keys']) == 6
        np.testing.assert_array_equal(mode['centroid'], [1., 0.])
