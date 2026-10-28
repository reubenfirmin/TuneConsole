import sqlite3

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


def test_choice_learning_keeps_counts_distinct_and_uses_last_evidence_time(store):
    store.modes.replace_modes([_mode(mid, "house", [["house", 1]], [1], 10, [])
                               for mid in range(1, 5)], [], 1)
    store.modes.log_impressions(1, [("a", 1), ("b", 1), ("c", 2), ("d", 4)], 10)
    store.modes.log_impressions(1, [("a", 1)], 100)  # re-render is not new evidence
    store.modes.log_pick(1, 1, 20)
    store.modes.log_pick(2, 1, 30)
    store.modes.log_pick(2, 1, 100)  # repeating a successful creation is idempotent
    store.modes.log_impressions(2, [("a", 2)], 40)
    store.modes.log_pick(3, 3, 50)  # retain an orphan pick without inventing an offer
    store.modes.replace_modes([], [4], 100)
    rows = store.modes.choice_learning_rows()
    assert [(r["mode_id"], r["offered"], r["picked"], r["updated_at"]) for r in rows] == [
        (3, 0, 1, 50), (2, 2, 0, 40), (1, 2, 2, 30)]


@pytest.mark.parametrize('file_backed', [False, True])
@pytest.mark.parametrize('enclosing', [False, True])
def test_failed_impression_batch_rolls_back_and_leaves_analytics_usable(tmp_path, file_backed, enclosing):
    store = Store(tmp_path / 'library.db' if file_backed else ':memory:')
    store.init_schema()
    try:
        store.modes.replace_modes([_mode(1, 'house', [], [1], 10, [])], [], 1)
        store.modes.log_impressions(0, [('wheelhouse', 1)], 9)
        if enclosing:
            store.conn.execute('BEGIN')
            store.conn.execute("INSERT INTO settings(key,value) VALUES('outer-write','retained')")
        with pytest.raises(sqlite3.IntegrityError):
            store.modes.log_impressions(1, [('wheelhouse', 1), ('explore', 999)], 10)
        assert store.conn.in_transaction == enclosing
        assert [tuple(row) for row in store.conn.execute(
            'SELECT epoch,lane,mode_id FROM rec_mode_impressions')] == [(0, 'wheelhouse', 1)]
        if enclosing:
            assert store.conn.execute("SELECT value FROM settings WHERE key='outer-write'").fetchone()[0] == 'retained'
            # Successful logging must also remain inside the caller's transaction.
            store.modes.log_impressions(2, [('comfort', 1)], 10)
            assert store.conn.in_transaction
            store.conn.rollback()
            assert store.conn.execute("SELECT value FROM settings WHERE key='outer-write'").fetchone() is None
        assert not store.conn.in_transaction
        assert store.modes.impression_counts() == {1: 1}
        # A leaked write transaction blocks committed-data readers in memory.
        assert store.rec_proposals.get_proposals('mode_bundles') is None
        store.modes.log_impressions(1, [('wheelhouse', 1)], 11)
        assert store.modes.impression_counts() == {1: 2}
    finally:
        store.conn.close()


def test_genres_and_meta_for(store):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,'a|x','Song A','Artist A',genre='house')
    seed_catalog_track(store,'b|y','Song B','Artist B',genre='')
    assert store.modes.genres_for(["a|x", "b|y", "c|z"]) == {"a|x": "house"}
    meta = store.modes.meta_for(["a|x"])
    assert meta["a|x"] == {"title": "Song A", "artist": "Artist A"}
