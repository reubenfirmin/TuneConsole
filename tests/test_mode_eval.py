import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import mode_eval


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _seed_modes(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "house", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
        {"mode_id": 2, "label": "techno", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 40, "rep_keys": []},
    ], retired_ids=[], now=1.0)


def test_scoreboard_offered_picked_plays(monkeypatch, store):
    _seed_modes(store)
    store.modes.log_impressions(1, [("wheelhouse", 1), ("explore", 2)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)     # mode 1 offered twice
    store.modes.log_pick(playlist_id=10, mode_id=1, now=120.0)         # mode 1 picked once
    monkeypatch.setattr(store.charts, "get_playlist_listen_stats",
                        lambda: {10: (999.0, 7)})
    board = {b["mode_id"]: b for b in mode_eval.mode_scoreboard(store)}
    assert board[1]["offered"] == 2 and board[1]["picked"] == 1 and board[1]["plays"] == 7
    assert board[1]["last_play"] == 999.0
    assert board[2]["offered"] == 1 and board[2]["picked"] == 0 and board[2]["plays"] == 0


def test_scoreboard_orders_by_offered(store):
    _seed_modes(store)
    store.modes.log_impressions(1, [("wheelhouse", 2)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 2)], now=110.0)
    store.modes.log_impressions(3, [("wheelhouse", 1)], now=120.0)
    order = [b["mode_id"] for b in mode_eval.mode_scoreboard(store)]
    assert order[0] == 2     # mode 2 offered more
