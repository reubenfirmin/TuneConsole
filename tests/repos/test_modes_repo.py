import numpy as np
import pytest
from yt_playlist.core.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _mode(mid, label, fams, vec, size, reps):
    return {"mode_id": mid, "label": label, "families": fams,
            "centroid": np.asarray(vec, dtype=np.float32), "size": size, "rep_keys": reps}


def test_replace_and_list_roundtrip(store):
    store.modes.replace_modes(
        [_mode(1, "house", [["house", 80], ["techno", 20]], [1.0, 0.0, 0.0], 100, ["a|x", "b|y"])],
        retired_ids=[], now=500.0)
    rows = store.modes.list_modes()
    assert len(rows) == 1
    r = rows[0]
    assert r["mode_id"] == 1 and r["label"] == "house" and r["size"] == 100
    assert r["families"] == [["house", 80], ["techno", 20]]
    assert r["rep_keys"] == ["a|x", "b|y"]
    assert np.allclose(r["centroid"], np.array([1.0, 0.0, 0.0], dtype=np.float32), atol=1e-6)
    assert r["first_seen"] == 500.0 and r["last_seen"] == 500.0 and r["active"] == 1


def test_first_seen_preserved_last_seen_advances(store):
    store.modes.replace_modes([_mode(1, "house", [["house", 80]], [1, 0], 100, [])], [], 500.0)
    store.modes.replace_modes([_mode(1, "house", [["house", 90]], [1, 0], 110, [])], [], 900.0)
    r = store.modes.list_modes()[0]
    assert r["first_seen"] == 500.0 and r["last_seen"] == 900.0 and r["size"] == 110


def test_retire_drops_from_active_list(store):
    store.modes.replace_modes(
        [_mode(1, "house", [["house", 1]], [1, 0], 50, []),
         _mode(2, "rock", [["rock", 1]], [0, 1], 40, [])], [], 500.0)
    store.modes.replace_modes([_mode(1, "house", [["house", 1]], [1, 0], 55, [])],
                              retired_ids=[2], now=900.0)
    active = store.modes.list_modes(active_only=True)
    assert [r["mode_id"] for r in active] == [1]
    allrows = store.modes.list_modes(active_only=False)
    assert {r["mode_id"]: r["active"] for r in allrows} == {1: 1, 2: 0}


def test_next_mode_id_spans_retired(store):
    store.modes.replace_modes([_mode(1, "a", [["a", 1]], [1], 50, []),
                               _mode(2, "b", [["b", 1]], [1], 50, [])], [], 500.0)
    store.modes.replace_modes([_mode(1, "a", [["a", 1]], [1], 50, [])], retired_ids=[2], now=900.0)
    assert store.modes.next_mode_id() == 3   # not 2: retired ids are never reused


def test_genres_and_meta_for(store):
    store.conn.executescript(
        "INSERT INTO tracks (identity_key, title, artist, genre) VALUES "
        "('a|x', 'Song A', 'Artist A', 'house'),"
        "('b|y', 'Song B', 'Artist B', '');")
    store.conn.commit()
    assert store.modes.genres_for(["a|x", "b|y", "c|z"]) == {"a|x": "house"}
    meta = store.modes.meta_for(["a|x"])
    assert meta["a|x"] == {"title": "Song A", "artist": "Artist A"}
