import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import embed, taste_modes as tm


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


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


def test_discover_modes_finds_planted(monkeypatch, store):
    keys, V, idx, blocks = _planted()
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    # genres_for returns the planted family as the genre; genre_map.family maps a genre to its family.
    monkeypatch.setattr(store.modes, "genres_for",
                        lambda ks: {k: k.split("|")[0] for k in ks})
    monkeypatch.setattr(tm.genre_map, "family", lambda g: g)
    # k=3 matches the 3 trivially-separable planted blocks, so recovery is seed-robust (k>3 would let
    # k-means legitimately split a tight blob into two dense clusters for some seeds).
    modes = tm.discover_modes(store, k=3, min_members=20, n_rep=3)
    assert len(modes) == 3
    labels = sorted(m["label"] for m in modes)
    assert labels == ["house", "jazz", "rock"]
    for m in modes:
        assert m["size"] >= 20
        assert np.isclose(np.linalg.norm(m["centroid"]), 1.0, atol=1e-5)
        assert len(m["rep_keys"]) == 3
        # representative keys belong to the same planted block as the label
        assert all(k.split("|")[0] == m["label"] for k in m["rep_keys"])


def test_discover_modes_cold_start(monkeypatch, store):
    monkeypatch.setattr(embed, "load_content_vectors",
                        lambda s: (["a|1"], np.ones((1, 4), dtype=np.float32), {"a|1": 0}))
    assert tm.discover_modes(store) == []
