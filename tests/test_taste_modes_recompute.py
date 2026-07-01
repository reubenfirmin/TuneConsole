import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import embed, taste_modes as tm


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _vectors(blocks, n_per=50, d=6):
    rng = np.random.Generator(np.random.PCG64(3))
    keys, rows = [], []
    for bi, base in enumerate(blocks):
        for i in range(n_per):
            v = np.array(base, dtype=np.float64) + rng.normal(0, 0.02, size=d)
            v /= np.linalg.norm(v)
            keys.append(f"b{bi}|{i}")
            rows.append(v.astype(np.float32))
    V = np.stack(rows)
    return keys, V, {k: i for i, k in enumerate(keys)}


def _wire(monkeypatch, store, keys, V, idx):
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    monkeypatch.setattr(store.modes, "genres_for", lambda ks: {k: k.split("|")[0] for k in ks})
    monkeypatch.setattr(tm.genre_map, "family", lambda g: g)


def test_recompute_populates(monkeypatch, store):
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    n = tm.recompute(store, now=1000.0, k=3, min_members=20)
    assert n == 3
    assert len(store.modes.list_modes()) == 3


def test_recompute_preserves_ids_then_retires(monkeypatch, store):
    blocks = [[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]]
    keys, V, idx = _vectors(blocks)
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    ids_first = {m["label"]: m["mode_id"] for m in store.modes.list_modes()}

    # Second run: identical data -> same clustering -> ids preserved.
    keys2, V2, idx2 = _vectors(blocks)
    _wire(monkeypatch, store, keys2, V2, idx2)
    tm.recompute(store, now=2000.0, k=3, min_members=20)
    ids_second = {m["label"]: m["mode_id"] for m in store.modes.list_modes()}
    assert ids_second == ids_first

    # Third run: drop the third block (k=2 for 2 blocks) -> its mode retires, survivors keep ids.
    keys3, V3, idx3 = _vectors(blocks[:2])
    _wire(monkeypatch, store, keys3, V3, idx3)
    tm.recompute(store, now=3000.0, k=2, min_members=20)
    active = store.modes.list_modes(active_only=True)
    assert len(active) == 2
    for m in active:
        assert m["mode_id"] == ids_first[m["label"]]
    assert len(store.modes.list_modes(active_only=False)) == 3   # retired one kept for history


def test_recompute_cold_start_no_crash(monkeypatch, store):
    monkeypatch.setattr(embed, "load_content_vectors",
                        lambda s: (["a|1"], np.ones((1, 4), dtype=np.float32), {"a|1": 0}))
    assert tm.recompute(store, now=1000.0) == 0
    assert store.modes.list_modes() == []


def test_rebuild_wires_recompute_under_a_guard():
    # The heavy end-to-end rebuild needs YouTube clients (covered elsewhere), so pin the wiring
    # structurally: _do_rebuild must call taste_modes.recompute, and the call must sit inside a
    # try/except so a mode failure cannot break the rebuild.
    import inspect
    from yt_playlist.rec import rec_worker
    src = inspect.getsource(rec_worker.RecWorker._do_rebuild)
    assert "taste_modes.recompute" in src
    # the recompute call appears after a `try:` (guarded), not at the top level of the method
    assert src.index("try:") < src.index("taste_modes.recompute")
