import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import mode_eval


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_ranker_scoreboard_aggregates_ab(monkeypatch, store):
    # ppr offered 2, cosine offered 1
    store.modes.log_impressions(1, [("wheelhouse", 1, "ppr"), ("explore", 2, "ppr")], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1, "cosine")], now=110.0)
    # ppr picked twice (playlists 10, 11), cosine picked once (playlist 12)
    store.modes.log_pick(10, 1, 120.0, ranker="ppr")
    store.modes.log_pick(11, 2, 121.0, ranker="ppr")
    store.modes.log_pick(12, 1, 122.0, ranker="cosine")
    # listen stats: playlist 10 -> 5 plays, 11 -> 3, 12 -> 1  =>  ppr plays 8, cosine plays 1
    monkeypatch.setattr(store.charts, "get_playlist_listen_stats",
                        lambda: {10: (200.0, 5), 11: (201.0, 3), 12: (202.0, 1)})

    board = {r["ranker"]: r for r in mode_eval.ranker_scoreboard(store)}
    assert board["ppr"] == {"ranker": "ppr", "offered": 2, "picked": 2, "plays": 8}
    assert board["cosine"] == {"ranker": "cosine", "offered": 1, "picked": 1, "plays": 1}


def test_ranker_scoreboard_empty(store):
    assert mode_eval.ranker_scoreboard(store) == []
