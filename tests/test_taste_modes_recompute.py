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


# --- content-space rebuild -----------------------------------------------------------------------
# Regression: rec_worker rebuilds the content space and then immediately recomputes modes, so the
# persisted centroids are from the previous space. Reconciling across the two raised
# "matmul: Input operand 1 has a mismatch in its core dimension" inside a best-effort try/except,
# which left taste modes silently frozen at their last-good value.

def _set_model(store, cat_tokens, cont=()):
    import json
    store.set_setting("rec_content_model", json.dumps(
        {"cat": {t: i for i, t in enumerate(cat_tokens)}, "ncat": len(cat_tokens),
         "cont": [[c, 0.0, 1.0] for c in cont]}))


def test_recompute_survives_a_content_space_rebuild(monkeypatch, store):
    """A wider space must not blow up, and must re-express every centroid in the new space."""
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    assert tm.recompute(store, now=1000.0, k=3, min_members=20) == 3

    # enrichment adds a token: the space is rebuilt one column wider, exactly as in the crash
    _set_model(store, [f"g{i}" for i in range(7)])
    keys7, V7, idx7 = _vectors([[1, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0, 0]], d=7)
    _wire(monkeypatch, store, keys7, V7, idx7)
    n = tm.recompute(store, now=2000.0, k=3, min_members=20)    # must not raise

    assert n == 3
    live = store.modes.list_modes(active_only=True)
    assert len(live) == 3
    assert all(m["centroid"].shape == (7,) for m in live)       # centroids are in the NEW space
    assert all(m["space"] for m in live)                        # and stamped with it


def test_recompute_carries_mode_ids_across_a_space_rebuild(monkeypatch, store):
    """The same taste regions, re-expressed in a wider space, must keep their ids: every pick,
    impression and Thompson posterior is keyed to mode_id (see rec/mode_eval.mode_bandit_stats)."""
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    old_ids = sorted(m["mode_id"] for m in store.modes.list_modes(active_only=True))

    _set_model(store, [f"g{i}" for i in range(7)])              # same tracks, wider space
    keys7, V7, idx7 = _vectors([[1, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0, 0]], d=7)
    _wire(monkeypatch, store, keys7, V7, idx7)
    tm.recompute(store, now=2000.0, k=3, min_members=20)

    assert sorted(m["mode_id"] for m in store.modes.list_modes(active_only=True)) == old_ids
    assert not [m for m in store.modes.list_modes(active_only=False) if not m["active"]]


def test_recompute_retires_ids_when_the_library_actually_changes(monkeypatch, store):
    """Containment matching must not resurrect an id for a genuinely different taste region."""
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    old_ids = sorted(m["mode_id"] for m in store.modes.list_modes(active_only=True))

    _set_model(store, [f"g{i}" for i in range(7)])
    keys7, V7, idx7 = _vectors([[1, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0, 0]], d=7)
    keys7 = [f"brandnew|{k}" for k in keys7]                    # an entirely different library
    idx7 = {k: i for i, k in enumerate(keys7)}
    _wire(monkeypatch, store, keys7, V7, idx7)
    tm.recompute(store, now=2000.0, k=3, min_members=20)

    live = {m["mode_id"] for m in store.modes.list_modes(active_only=True)}
    assert not (set(old_ids) & live)                            # no id survives a disjoint library
    retired = [m for m in store.modes.list_modes(active_only=False) if not m["active"]]
    assert sorted(m["mode_id"] for m in retired) == old_ids


def test_recompute_keeps_ids_when_the_space_is_unchanged(monkeypatch, store):
    """The fingerprint must not be so strict that a normal rebuild churns every mode_id."""
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    before = sorted(m["mode_id"] for m in store.modes.list_modes())
    tm.recompute(store, now=2000.0, k=3, min_members=20)
    assert sorted(m["mode_id"] for m in store.modes.list_modes()) == before


# --- live_modes: the safe read of persisted centroids ---------------------------------------------
# enrich_worker rebuilds the content space without recomputing modes, so between the two the stored
# centroids belong to a space that no longer exists. Every consumer that stacks them against live
# content vectors (ppr, layers, radio, mode_surfaces.prepare_bundles) reads through live_modes.

def test_live_modes_hides_modes_from_a_superseded_space(monkeypatch, store):
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    assert len(tm.live_modes(store)) == 3

    _set_model(store, [f"g{i}" for i in range(7)])          # enrichment rebuilds the space
    assert store.modes.list_modes(active_only=True) != []   # rows are still there, still active
    assert tm.live_modes(store) == []                       # ...but none are usable against it


def test_live_modes_returns_modes_in_the_current_space(monkeypatch, store):
    _set_model(store, [f"g{i}" for i in range(6)])
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    live = tm.live_modes(store)
    assert len(live) == 3
    from yt_playlist.rec import embed as _e
    assert {m["space"] for m in live} == {_e.content_space_id(store)}


def test_live_modes_on_a_store_with_no_content_model(monkeypatch, store):
    """Fresh install: no model persisted, modes stamped ''. Both sides agree, nothing is hidden."""
    keys, V, idx = _vectors([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0]])
    _wire(monkeypatch, store, keys, V, idx)
    tm.recompute(store, now=1000.0, k=3, min_members=20)
    assert len(tm.live_modes(store)) == 3
